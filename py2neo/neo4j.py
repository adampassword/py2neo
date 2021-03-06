#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2011-2014, Nigel Small
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" The neo4j module provides the main `Neo4j <http://neo4j.org/>`_ client
functionality and will be the starting point for most applications. The main
classes provided are:

- :py:class:`Graph` - an instance of a Neo4j database server,
  providing a number of graph-global methods for handling nodes and
  relationships
- :py:class:`Node` - a representation of a database node
- :py:class:`Relationship` - a representation of a relationship between two
  database nodes
- :py:class:`Path` - a sequence of alternating nodes and relationships
- :py:class:`ReadBatch` - a batch of read requests to be carried out within a
  single transaction
- :py:class:`WriteBatch` - a batch of write requests to be carried out within
  a single transaction
"""


from __future__ import division, unicode_literals

import base64
import json
import logging
import re
from weakref import WeakValueDictionary

from py2neo import __version__
from py2neo.error import ClientError, ServerError, ServerException, UnboundError, UnjoinableError
from py2neo.packages.httpstream import (
    http, Resource as _Resource, ResourceTemplate as _ResourceTemplate,
    ClientError as _ClientError, ServerError as _ServerError)
from py2neo.packages.httpstream.numbers import NOT_FOUND, CONFLICT, BAD_REQUEST
from py2neo.packages.jsonstream import assembled, grouped
from py2neo.packages.urimagic import percent_encode, URI, URITemplate
from py2neo.util import compact, deprecated, flatten, has_all, is_collection, is_integer, \
    round_robin, ustr, version_tuple


__all__ = ["DEFAULT_URI", "Graph", "GraphDatabaseService", "Node", "NodePointer", "Path", "Rel", "Rev", "Relationship",
           "ReadBatch", "WriteBatch", "BatchRequestList", "_cast",
           "Index", "LegacyReadBatch", "LegacyWriteBatch",
           "UnjoinableError"]


DEFAULT_SCHEME = "http"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 7474
DEFAULT_HOST_PORT = "{0}:{1}".format(DEFAULT_HOST, DEFAULT_PORT)
DEFAULT_URI = "{0}://{1}".format(DEFAULT_SCHEME, DEFAULT_HOST_PORT)  # TODO: remove - moved to ServiceRoot

PRODUCT = ("py2neo", __version__)

NON_ALPHA_NUM = re.compile("[^0-9A-Za-z_]")
SIMPLE_NAME = re.compile(r"[A-Za-z_][0-9A-Za-z_]*")

http.default_encoding = "UTF-8"

# TODO: put these in other modules
batch_log = logging.getLogger(__name__ + ".batch")
cypher_log = logging.getLogger(__name__ + ".cypher")

_headers = {
    None: [("X-Stream", "true")]
}

_http_rewrites = {}

auto_sync = True


def _add_header(key, value, host_port=None):
    """ Add an HTTP header to be sent with all requests if no `host_port`
    is provided or only to those matching the value supplied otherwise.
    """
    if host_port in _headers:
        _headers[host_port].append((key, value))
    else:
        _headers[host_port] = [(key, value)]


def _get_headers(host_port):
    """Fetch all HTTP headers relevant to the `host_port` provided.
    """
    uri_headers = {}
    for n, headers in _headers.items():
        if n is None or n == host_port:
            uri_headers.update(headers)
    return uri_headers


def authenticate(host_port, user_name, password):
    """ Set HTTP basic authentication values for specified `host_port`. The
    code below shows a simple example::

        # set up authentication parameters
        neo4j.authenticate("camelot:7474", "arthur", "excalibur")

        # connect to authenticated graph database
        graph = neo4j.Graph("http://camelot:7474/db/data/")

    Note: a `host_port` can be either a server name or a server name and port
    number but must match exactly that used within the Graph
    URI.

    :param host_port: the host and optional port requiring authentication
        (e.g. "bigserver", "camelot:7474")
    :param user_name: the user name to authenticate as
    :param password: the password
    """
    credentials = (user_name + ":" + password).encode("UTF-8")
    value = "Basic " + base64.b64encode(credentials).decode("ASCII")
    _add_header("Authorization", value, host_port=host_port)


def familiar(*resources):
    """ Return :py:const:`True` if all resources share a common service root.

    :param resources:
    :return:
    """
    if len(resources) < 2:
        return True
    return all(_.service_root == resources[0].service_root for _ in resources)


def rewrite(from_scheme_host_port, to_scheme_host_port):
    """ Automatically rewrite all URIs directed to the scheme, host and port
    specified in `from_scheme_host_port` to that specified in
    `to_scheme_host_port`.

    As an example::

        # implicitly convert all URIs beginning with <http://localhost:7474>
        # to instead use <https://dbserver:9999>
        neo4j.rewrite(("http", "localhost", 7474), ("https", "dbserver", 9999))

    If `to_scheme_host_port` is :py:const:`None` then any rewrite rule for
    `from_scheme_host_port` is removed.

    This facility is primarily intended for use by database servers behind
    proxies which are unaware of their externally visible network address.
    """
    global _http_rewrites
    if to_scheme_host_port is None:
        try:
            del _http_rewrites[from_scheme_host_port]
        except KeyError:
            pass
    else:
        _http_rewrites[from_scheme_host_port] = to_scheme_host_port


class Resource(_Resource):
    """ Variant of HTTPStream Resource that passes extra headers and product
    detail.
    """

    def __init__(self, uri, metadata=None):
        uri = URI(uri)
        scheme_host_port = (uri.scheme, uri.host, uri.port)
        if scheme_host_port in _http_rewrites:
            scheme_host_port = _http_rewrites[scheme_host_port]
            # This is fine - it's all my code anyway...
            uri._URI__set_scheme(scheme_host_port[0])
            uri._URI__set_authority("{0}:{1}".format(scheme_host_port[1],
                                                     scheme_host_port[2]))
        if uri.user_info:
            authenticate(uri.host_port, *uri.user_info.partition(":")[0::2])
        self._resource = _Resource.__init__(self, uri)
        #self._subresources = {}
        self.__headers = _get_headers(self.__uri__.host_port)
        self.__base = super(Resource, self)
        if metadata is None:
            self.__initial_metadata = None
        else:
            self.__initial_metadata = dict(metadata)
        self.__last_get_response = None

        uri = uri.string
        service_root_uri = uri[:uri.find("/", uri.find("//") + 2)] + "/"
        if service_root_uri == uri:
            self.__service_root = self
        else:
            self.__service_root = ServiceRoot(service_root_uri)

    @property
    def headers(self):
        return self.__headers

    @property
    def service_root(self):
        return self.__service_root

    @property
    def metadata(self):
        if self.__last_get_response is None:
            if self.__initial_metadata is not None:
                return self.__initial_metadata
            self.get()
        return self.__last_get_response.content

    def get(self, headers=None, redirect_limit=5, **kwargs):
        headers = dict(headers or {})
        headers.update(self.__headers)
        kwargs.update(product=PRODUCT, cache=True)
        # TODO: clean up exception handling - decorator? do we need both client/server types at this level?
        try:
            self.__last_get_response = self.__base.get(headers, redirect_limit, **kwargs)
        except _ClientError as err:
            raise ClientError(err)
        except _ServerError as err:
            raise ServerError(err)
        else:
            return self.__last_get_response

    def put(self, body=None, headers=None, **kwargs):
        headers = dict(headers or {})
        headers.update(self.__headers)
        kwargs.update(product=PRODUCT)
        try:
            response = self.__base.put(body, headers, **kwargs)
        except _ClientError as err:
            raise ClientError(err)
        except _ServerError as err:
            raise ServerError(err)
        else:
            return response

    def post(self, body=None, headers=None, **kwargs):
        headers = dict(headers or {})
        headers.update(self.__headers)
        kwargs.update(product=PRODUCT)
        try:
            response = self.__base.post(body, headers, **kwargs)
        except _ClientError as err:
            raise ClientError(err)
        except _ServerError as err:
            raise ServerError(err)
        else:
            return response

    def delete(self, headers=None, **kwargs):
        headers = dict(headers or {})
        headers.update(self.__headers)
        kwargs.update(product=PRODUCT)
        try:
            response = self.__base.delete(headers, **kwargs)
        except _ClientError as err:
            raise ClientError(err)
        except _ServerError as err:
            raise ServerError(err)
        else:
            return response


class ResourceTemplate(_ResourceTemplate):

    def expand(self, **values):
        return Resource(self.uri_template.expand(**values))


class Bindable(object):
    """ Base class for objects that can be bound to a remote resource.
    """

    def __init__(self, uri=None):
        self.__resource = None
        if uri:
            self.bind(uri)

    @property
    def service_root(self):
        return self.resource.service_root

    @property
    def graph(self):
        return self.service_root.graph

    @property
    def uri(self):
        return self.resource.uri

    @property
    def resource(self):
        """ Returns the :class:`Resource` to which this is bound.
        """
        if self.bound:
            return self.__resource
        else:
            raise UnboundError("Local object is not bound to a "
                               "remote resource")

    @property
    def bound(self):
        """ Returns :const:`True` if bound to a remote resource.
        """
        return self.__resource is not None

    def bind(self, uri, metadata=None):
        """ Bind object to Resource or ResourceTemplate.
        """
        if "{" in uri:
            if metadata:
                raise ValueError("Initial metadata cannot be stored for a "
                                 "resource template")
            self.__resource = ResourceTemplate(uri)
        else:
            self.__resource = Resource(uri, metadata)

    def unbind(self):
        self.__resource = None

    # deprecated
    @property
    def is_abstract(self):
        return not self.bound


class ServiceRoot(object):
    """ Neo4j REST API service root resource.
    """

    DEFAULT_URI = "{0}://{1}".format(DEFAULT_SCHEME, DEFAULT_HOST_PORT)

    __instances = {}

    def __new__(cls, uri=None):
        """ Fetch a cached instance if one is available, otherwise create,
        cache and return a new instance.

        :param uri: URI of the cached resource
        :return: a resource instance
        """
        inst = super(ServiceRoot, cls).__new__(cls)
        return cls.__instances.setdefault(uri, inst)

    def __init__(self, uri=None):
        self.__resource = Resource(uri or self.DEFAULT_URI)

    @property
    def resource(self):
        return self.__resource

    @property
    def graph(self):
        return Graph(self.resource.metadata["data"])

    @property
    def uri(self):
        return self.resource.uri


class Graph(Bindable):
    """ An instance of a `Neo4j <http://neo4j.org/>`_ database identified by
    its base URI. Generally speaking, this is the only URI which a system
    attaching to this service should need to be directly aware of; all further
    entity URIs will be discovered automatically from within response content
    when possible (see `Hypermedia <http://en.wikipedia.org/wiki/Hypermedia>`_)
    or will be derived from existing URIs.

    The following code illustrates how to connect to a database server and
    display its version number::

        from py2neo import Graph
        
        graph = Graph()
        print(graph.neo4j_version)

    :param uri: the base URI of the database (defaults to <http://localhost:7474/db/data/>)
    """

    __instances = {}

    @staticmethod
    def cast(obj):
        if obj is None:
            return None
        elif isinstance(obj, (Node, NodePointer, Path, Rel, Relationship, Rev)):
            return obj
        elif isinstance(obj, dict):
            return Node.cast(obj)
        elif isinstance(obj, tuple):
            return Relationship.cast(obj)
        else:
            raise TypeError(obj)

    def __new__(cls, uri=None):
        """ Fetch a cached instance if one is available, otherwise create,
        cache and return a new instance.

        :param uri: URI of the cached resource
        :return: a resource instance
        """
        inst = super(Graph, cls).__new__(cls)
        return cls.__instances.setdefault((cls, uri), inst)

    def __init__(self, uri=None):
        if uri is None:
            uri = ServiceRoot().graph.resource.uri
        Bindable.__init__(self, uri)
        self.__node_cache = WeakValueDictionary()
        self.__rel_cache = WeakValueDictionary()

    def __len__(self):
        """ Return the size of this graph (i.e. the number of relationships).
        """
        return self.size

    def __bool__(self):
        return True

    def __nonzero__(self):
        return True

    def clear(self):
        """ Clear all nodes and relationships from the graph.

        .. warning::
            This method will permanently remove **all** nodes and relationships
            from the graph and cannot be undone.
        """
        batch = WriteBatch(self)
        batch.append_cypher("START r=rel(*) DELETE r")
        batch.append_cypher("START n=node(*) DELETE n")
        batch.run()

    # TODO: pass out same objects passed in
    def create(self, *abstracts):
        """ Create multiple nodes and/or relationships as part of a single
        batch.

        The abstracts provided may use any accepted notation, as described in
        the section on py2neo fundamentals.
        For a node, simply pass a dictionary of properties; for a relationship, pass a tuple of
        (start, type, end) or (start, type, end, data) where start and end
        may be :py:class:`Node` instances or zero-based integral references
        to other node entities within this batch::

            # create a single node
            alice, = graph.create({"name": "Alice"})

            # create multiple nodes
            people = graph.create(
                {"name": "Alice", "age": 33}, {"name": "Bob", "age": 44},
                {"name": "Carol", "age": 55}, {"name": "Dave", "age": 66},
            )

            # create two nodes with a connecting relationship
            alice, bob, ab = graph.create(
                {"name": "Alice"}, {"name": "Bob"},
                (0, "KNOWS", 1, {"since": 2006})
            )

            # create a node plus a relationship to pre-existing node
            bob, ab = graph.create({"name": "Bob"}, (alice, "PERSON", 0))

        :return: list of :py:class:`Node` and/or :py:class:`Relationship`
            instances

        .. warning::
            This method will *always* return a list, even when only creating
            a single node or relationship. To automatically unpack a list
            containing a single item, append a trailing comma to the variable
            name on the left of the assignment operation.

        """
        if not abstracts:
            return []
        batch = WriteBatch(self)
        for abstract in abstracts:
            batch.create(abstract)
        return batch.submit()

    def delete(self, *entities):
        """ Delete multiple nodes and/or relationships as part of a single
        batch.
        """
        if not entities:
            return
        batch = WriteBatch(self)
        for entity in entities:
            if entity is not None:
                batch.delete(entity)
        batch.run()

    def find(self, label, property_key=None, property_value=None):
        """ Iterate through a set of labelled nodes, optionally filtering
        by property key and value
        """
        uri = self.resource.uri.resolve("/".join(["label", label, "nodes"]))
        if property_key:
            uri = uri.resolve("?" + percent_encode({property_key: json.dumps(property_value, ensure_ascii=False)}))
        try:
            for i, result in grouped(Resource(uri).get()):
                yield self.hydrate(assembled(result))
        except ClientError as err:
            if err.status_code != NOT_FOUND:
                raise

    # TODO: replace with PullBatch
    def get_properties(self, *entities):
        """ Fetch properties for multiple nodes and/or relationships as part
        of a single batch; returns a list of dictionaries in the same order
        as the supplied entities.
        """
        if not entities:
            return []
        if len(entities) == 1:
            return [entities[0].get_properties()]
        batch = BatchRequestList(self, hydrate=False)
        for entity in entities:
            batch.append_get(batch._uri_for(entity, "properties"))
        return [properties or {} for properties in batch.submit()]

    def match(self, start_node=None, rel_type=None, end_node=None,
              bidirectional=False, limit=None):
        """ Iterate through all relationships matching specified criteria.

        Examples are as follows::

            # all relationships from the graph database
            # ()-[r]-()
            rels = list(graph.match())

            # all relationships outgoing from `alice`
            # (alice)-[r]->()
            rels = list(graph.match(start_node=alice))

            # all relationships incoming to `alice`
            # ()-[r]->(alice)
            rels = list(graph.match(end_node=alice))

            # all relationships attached to `alice`, regardless of direction
            # (alice)-[r]-()
            rels = list(graph.match(start_node=alice, bidirectional=True))

            # all relationships from `alice` to `bob`
            # (alice)-[r]->(bob)
            rels = list(graph.match(start_node=alice, end_node=bob))

            # all relationships outgoing from `alice` of type "FRIEND"
            # (alice)-[r:FRIEND]->()
            rels = list(graph.match(start_node=alice, rel_type="FRIEND"))

            # up to three relationships outgoing from `alice` of type "FRIEND"
            # (alice)-[r:FRIEND]->()
            rels = list(graph.match(start_node=alice, rel_type="FRIEND", limit=3))

        :param start_node: concrete start :py:class:`Node` to match or
            :py:const:`None` if any
        :param rel_type: type of relationships to match or :py:const:`None` if
            any
        :param end_node: concrete end :py:class:`Node` to match or
            :py:const:`None` if any
        :param bidirectional: :py:const:`True` if reversed relationships should
            also be included
        :param limit: maximum number of relationships to match or
            :py:const:`None` if no limit
        :return: matching relationships
        :rtype: generator
        """
        if start_node is None and end_node is None:
            query = "START a=node(*)"
            params = {}
        elif end_node is None:
            query = "START a=node({A})"
            start_node = Node.cast(start_node)
            if not start_node.bound:
                raise TypeError("Nodes for relationship match end points must be bound")
            params = {"A": start_node._id}
        elif start_node is None:
            query = "START b=node({B})"
            end_node = Node.cast(end_node)
            if not end_node.bound:
                raise TypeError("Nodes for relationship match end points must be bound")
            params = {"B": end_node._id}
        else:
            query = "START a=node({A}),b=node({B})"
            start_node = Node.cast(start_node)
            end_node = Node.cast(end_node)
            if not start_node.bound or not end_node.bound:
                raise TypeError("Nodes for relationship match end points must be bound")
            params = {"A": start_node._id, "B": end_node._id}
        if rel_type is None:
            rel_clause = ""
        elif is_collection(rel_type):
            if self.neo4j_version >= (2, 0, 0):
                # yuk, version sniffing :-(
                separator = "|:"
            else:
                separator = "|"
            rel_clause = ":" + separator.join("`{0}`".format(_)
                                              for _ in rel_type)
        else:
            rel_clause = ":`{0}`".format(rel_type)
        if bidirectional:
            query += " MATCH (a)-[r" + rel_clause + "]-(b) RETURN r"
        else:
            query += " MATCH (a)-[r" + rel_clause + "]->(b) RETURN r"
        if limit is not None:
            query += " LIMIT {0}".format(int(limit))
        results = CypherQuery(self, query).stream(**params)
        try:
            for result in results:
                yield result[0]
        finally:
            results.close()

    def match_one(self, start_node=None, rel_type=None, end_node=None,
                  bidirectional=False):
        """ Fetch a single relationship matching specified criteria.

        :param start_node: concrete start :py:class:`Node` to match or
            :py:const:`None` if any
        :param rel_type: type of relationships to match or :py:const:`None` if
            any
        :param end_node: concrete end :py:class:`Node` to match or
            :py:const:`None` if any
        :param bidirectional: :py:const:`True` if reversed relationships should
            also be included
        :return: a matching :py:class:`Relationship` or :py:const:`None`

        .. seealso::
           :py:func:`Graph.match <py2neo.neo4j.Graph.match>`
        """
        rels = list(self.match(start_node, rel_type, end_node,
                               bidirectional, 1))
        if rels:
            return rels[0]
        else:
            return None

    @property
    def neo4j_version(self):
        """ The database software version as a 4-tuple of (``int``, ``int``,
        ``int``, ``str``).
        """
        return version_tuple(self.resource.metadata["neo4j_version"])

    def node(self, id_):
        """ Fetch a node by ID.
        """
        # TODO: use cache
        resource = self.resource.resolve("node/" + str(id_))
        return self.hydrate(resource.get().content)

    @property
    def node_labels(self):
        """ The set of node labels currently defined within the graph.
        """
        resource = Resource(URI(self).resolve("labels"))
        try:
            return frozenset(self.hydrate(assembled(resource.get())))
        except ClientError as err:
            if err.status_code == NOT_FOUND:
                raise NotImplementedError("Node labels not available for this "
                                          "Neo4j server version")
            else:
                raise

    @property
    def order(self):
        """ The number of nodes in this graph.
        """
        return CypherQuery(self, "START n=node(*) "
                                 "RETURN count(n)").execute_one()

    def relationship(self, id_):
        """ Fetch a relationship by ID.
        """
        # TODO: use cache
        resource = self.resource.resolve("relationship/" + str(id_))
        return self.hydrate(resource.get().content)


    @property
    def relationship_types(self):
        """ The set of relationship types currently defined within the graph.
        """
        resource = Resource(self.resource.metadata["relationship_types"])
        return frozenset(self.hydrate(resource.get().content))

    @property
    def schema(self):
        """ The Schema resource for this graph.

        .. seealso::
            :py:func:`Schema <py2neo.neo4j.Schema>`
        """
        return Schema(URI(self).resolve("schema"))

    @property
    def size(self):
        """ The number of relationships in this graph.
        """
        return CypherQuery(self, "START r=rel(*) "
                                 "RETURN count(r)").execute_one()

    @property
    def supports_foreach_pipe(self):
        """ Indicates whether the server supports pipe syntax for FOREACH.
        """
        return self.neo4j_version >= (2, 0)

    @property
    def supports_node_labels(self):
        """ Indicates whether the server supports node labels.
        """
        return self.neo4j_version >= (2, 0)

    @property
    def supports_optional_match(self):
        """ Indicates whether the server supports Cypher OPTIONAL MATCH
        clauses.
        """
        return self.neo4j_version >= (2, 0)

    @property
    def supports_schema_indexes(self):
        """ Indicates whether the server supports schema indexes.
        """
        return self.neo4j_version >= (2, 0)

    @property
    def supports_cypher_transactions(self):
        """ Indicates whether the server supports explicit Cypher transactions.
        """
        return "transaction" in self.resource.metadata

    def relative_uri(self, uri):
        # "http://localhost:7474/db/data/", "node/1"
        # TODO: confirm is URI
        self_uri = self.resource.uri.string
        if uri.startswith(self_uri):
            return uri[len(self_uri):]
        else:
            # TODO: specialist error
            raise ValueError(uri + " does not belong to this graph")

    # TODO:  add support for CypherResults and BatchResponse
    def hydrate(self, data):
        if isinstance(data, dict):
            if "self" in data:
                # entity (node or rel)
                tag, i = self.relative_uri(data["self"]).partition("/")[0::2]
                if tag == "":
                    return self  # uri refers to graph
                elif tag == "node":
                    return self.__node_cache.setdefault(int(i), Node.hydrate(data))
                elif tag == "relationship":
                    return self.__rel_cache.setdefault(int(i), Relationship.hydrate(data))
                else:
                    raise ValueError("Cannot hydrate entity of type '{}'".format(tag))
            elif "nodes" in data and "relationships" in data:
                # path
                return Path.hydrate(data)
            elif has_all(data, ("exception", "stacktrace")):
                err = ServerException(data)
                raise BatchError.with_name(err.exception)(err)
            else:
                # TODO: warn about dict ambiguity
                return data
        elif is_collection(data):
            return type(data)(map(self.hydrate, data))
        else:
            return data


class CypherQuery(object):
    """ A reusable Cypher query. To create a new query object, a graph and the
    query text need to be supplied::

        >>> from py2neo import neo4j
        >>> graph = neo4j.Graph()
        >>> query = neo4j.CypherQuery(graph, "CREATE (a) RETURN a")

    """

    def __init__(self, graph, query):
        self._graph = graph
        self._cypher = Resource(graph.resource.metadata["cypher"])
        self._query = query

    def __str__(self):
        return self._query

    @property
    def string(self):
        """ The text of the query.
        """
        return self._query

    def _execute(self, **params):
        if __debug__:
            cypher_log.debug("Query: " + repr(self._query))
            if params:
                cypher_log.debug("Params: " + repr(params))
        try:
            return self._cypher.post({
                "query": self._query,
                "params": dict(params or {}),
            })
        except ClientError as e:
            if e.exception:
                # A CustomCypherError is a dynamically created subclass of
                # CypherError with the same name as the underlying server
                # exception
                CustomCypherError = type(str(e.exception), (CypherError,), {})
                raise CustomCypherError(e)
            else:
                raise CypherError(e)

    def run(self, **params):
        """ Execute the query and discard any results.

        :param params:
        """
        self._execute(**params).close()

    def execute(self, **params):
        """ Execute the query and return the results.

        :param params:
        :return:
        :rtype: :py:class:`CypherResults <py2neo.neo4j.CypherResults>`
        """
        return CypherResults(self._graph, self._execute(**params))

    def execute_one(self, **params):
        """ Execute the query and return the first value from the first row.

        :param params:
        :return:
        """
        try:
            return self.execute(**params).data[0][0]
        except IndexError:
            return None

    def stream(self, **params):
        """ Execute the query and return a result iterator.

        :param params:
        :return:
        :rtype: :py:class:`IterableCypherResults <py2neo.neo4j.IterableCypherResults>`
        """
        return IterableCypherResults(self._graph, self._execute(**params))


class CypherResults(object):
    """ A static set of results from a Cypher query.
    """

    # TODO
    @classmethod
    def _hydrated(cls, graph, data):
        """ Takes assembled data...
        """
        producer = RecordProducer(data["columns"])
        return [
            producer.produce(graph.hydrate(row))
            for row in data["data"]
        ]

    def __init__(self, graph, response):
        content = response.content
        self._columns = tuple(content["columns"])
        self._producer = RecordProducer(self._columns)
        self._data = [
            self._producer.produce(graph.hydrate(row))
            for row in content["data"]
        ]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def __len__(self):
        return len(self._data)

    def __getitem__(self, item):
        return self._data[item]

    @property
    def columns(self):
        """ Column names.
        """
        return self._columns

    @property
    def data(self):
        """ List of result records.
        """
        return self._data

    def __iter__(self):
        return iter(self._data)


class IterableCypherResults(object):
    """ An iterable set of results from a Cypher query.

    ::

        query = graph.cypher.query("START n=node(*) RETURN n LIMIT 10")
        for record in query.stream():
            print record[0]

    Each record returned is cast into a :py:class:`namedtuple` with names
    derived from the resulting column names.

    .. note ::
        Results are available as returned from the server and are decoded
        incrementally. This means that there is no need to wait for the
        entire response to be received before processing can occur.
    """

    def __init__(self, graph, response):
        self._graph = graph
        self._response = response
        self._redo_buffer = []
        self._buffered = self._buffered_results()
        self._columns = None
        self._fetch_columns()
        self._producer = RecordProducer(self._columns)

    def _fetch_columns(self):
        redo = []
        section = []
        for key, value in self._buffered:
            if key and key[0] == "columns":
                section.append((key, value))
            else:
                redo.append((key, value))
                if key and key[0] == "data":
                    break
        self._redo_buffer.extend(redo)
        self._columns = tuple(assembled(section)["columns"])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _buffered_results(self):
        for result in self._response:
            while self._redo_buffer:
                yield self._redo_buffer.pop(0)
            yield result

    def __iter__(self):
        for key, section in grouped(self._buffered):
            if key[0] == "data":
                for i, row in grouped(section):
                    yield self._producer.produce(self._graph.hydrate(assembled(row)))

    @property
    def columns(self):
        """ Column names.
        """
        return self._columns

    def close(self):
        """ Close results and free resources.
        """
        self._response.close()


class Schema(Bindable):

    __instances = {}

    def __new__(cls, uri=None):
        """ Fetch a cached instance if one is available, otherwise create,
        cache and return a new instance.

        :param uri: URI of the cached resource
        :return: a resource instance
        """
        inst = super(Schema, cls).__new__(cls, uri)
        return cls.__instances.setdefault(uri, inst)

    def __init__(self, uri):
        Bindable.__init__(self, uri)
        if not self.service_root.graph.supports_schema_indexes:
            raise NotImplementedError("Schema index support requires "
                                      "version 2.0 or above")
        self._index_template = \
            URITemplate(str(URI(self)) + "/index/{label}")
        self._index_key_template = \
            URITemplate(str(URI(self)) + "/index/{label}/{property_key}")
        self._uniqueness_constraint_template = \
            URITemplate(str(URI(self)) + "/constraint/{label}/uniqueness")
        self._uniqueness_constraint_key_template = \
            URITemplate(str(URI(self)) + "/constraint/{label}/uniqueness/{property_key}")

    def get_indexed_property_keys(self, label):
        """ Fetch a list of indexed property keys for a label.

        :param label:
        :return:
        """
        if not label:
            raise ValueError("Label cannot be empty")
        resource = Resource(self._index_template.expand(label=label))
        try:
            response = resource.get()
        except ClientError as err:
            if err.status_code == NOT_FOUND:
                return []
            else:
                raise
        else:
            return [
                indexed["property_keys"][0]
                for indexed in response.content
            ]

    def get_unique_constraints(self, label):
        """ Fetch a list of uniqueness constraints for a label.

        :param label:
        :return:
        """
        if not label:
            raise ValueError("Label cannot be empty")
        resource = Resource(self._uniqueness_constraint_template.expand(label=label))
        try:
            response = resource.get()
        except ClientError as err:
            if err.status_code == NOT_FOUND:
                return []
            else:
                raise
        else:
            return [
                unique["property_keys"][0]
                for unique in response.content
            ]

    def create_index(self, label, property_key):
        """ Index a property key for a label.

        :param label:
        :param property_key:
        :return:
        """
        if not label or not property_key:
            raise ValueError("Neither label nor property key can be empty")
        resource = Resource(self._index_template.expand(label=label))
        property_key = bytearray(property_key, "utf-8").decode("utf-8")
        try:
            resource.post({"property_keys": [property_key]})
        except ClientError as err:
            if err.status_code == CONFLICT:
                raise ValueError(err.cause.message)
            else:
                raise

    def add_unique_constraint(self, label, property_key):
        """ Create an uniqueness constraint for a label.

         :param label:
         :param property_key:
         :return:
        """

        if not label or not property_key:
            raise ValueError("Neither label nor property key can be empty")
        resource = Resource(self._uniqueness_constraint_template.expand(label=label))
        try:
            resource.post({"property_keys": [ustr(property_key)]})
        except ClientError as err:
            if err.status_code == CONFLICT:
                raise ValueError(err.cause.message)
            else:
                raise

    def drop_index(self, label, property_key):
        """ Remove label index for a given property key.

        :param label:
        :param property_key:
        :return:
        """
        if not label or not property_key:
            raise ValueError("Neither label nor property key can be empty")
        uri = self._index_key_template.expand(label=label,
                                              property_key=property_key)
        resource = Resource(uri)
        try:
            resource.delete()
        except ClientError as err:
            if err.status_code == NOT_FOUND:
                raise LookupError("Property key not found")
            else:
                raise

    def remove_unique_constraint(self, label, property_key):
        """ Remove uniqueness constraint for a given property key.

         :param label:
         :param property_key:
         :return:
        """
        if not label or not property_key:
            raise ValueError("Neither label nor property key can be empty")
        uri = self._uniqueness_constraint_key_template.expand(label=label,
                                                              property_key=property_key)
        resource = Resource(uri)
        try:
            resource.delete()
        except ClientError as err:
            if err.status_code == NOT_FOUND:
                raise LookupError("Property key not found")
            else:
                raise


class PropertySet(Bindable, dict):
    """ A dict subclass that equates None with a non-existent key and can be
    bound to a remote *properties* resource.
    """

    def __init__(self, iterable=None, **kwargs):
        Bindable.__init__(self)
        dict.__init__(self)
        self.update(iterable, **kwargs)

    def __getitem__(self, key):
        return dict.get(self, key)

    def __setitem__(self, key, value):
        if value is None:
            try:
                dict.__delitem__(self, key)
            except KeyError:
                pass
        else:
            dict.__setitem__(self, key, value)

    def __eq__(self, other):
        if not isinstance(other, PropertySet):
            other = PropertySet(other)
        return dict.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def setdefault(self, key, default=None):
        if key in self:
            value = self[key]
        elif default is None:
            value = None
        else:
            value = dict.setdefault(self, key, default)
        return value

    def update(self, iterable=None, **kwargs):
        if iterable:
            try:
                for key in iterable.keys():
                    self[key] = iterable[key]
            except (AttributeError, TypeError):
                for key, value in iterable:
                    self[key] = value
        for key in kwargs:
            self[key] = kwargs[key]

    def pull(self):
        """ Copy the set of remote properties onto the local set.
        """
        self.resource.get()
        self.clear()
        properties = self.resource.metadata
        if properties:
            self.update(properties)

    def push(self):
        """ Copy the set of local properties onto the remote set.
        """
        self.resource.put(self)

    def __json__(self):
        return json.dumps(self, separators=",:", sort_keys=True)


class LabelSet(Bindable, set):
    """ A set subclass that can be bound to a remote *labels* resource.
    """

    def __init__(self, iterable=None):
        Bindable.__init__(self)
        set.__init__(self)
        if iterable:
            self.update(iterable)

    def __eq__(self, other):
        if not isinstance(other, LabelSet):
            other = LabelSet(other)
        return set.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def pull(self):
        """ Copy the set of remote labels onto the local set.
        """
        self.resource.get()
        self.clear()
        labels = self.resource.metadata
        if labels:
            self.update(labels)

    def push(self):
        """ Copy the set of local labels onto the remote set.
        """
        self.resource.put(self)


class PropertyContainer(Bindable):
    """ Base class for objects that contain a set of properties,
    i.e. :py:class:`Node` and :py:class:`Relationship`.
    """

    def __init__(self, **properties):
        Bindable.__init__(self)
        self.__properties = PropertySet(properties)

    def __eq__(self, other):
        return self.properties == other.properties

    def __ne__(self, other):
        return not self.__eq__(other)

    def __len__(self):
        return len(self.properties)

    def __contains__(self, key):
        # TODO 2.0: remove auto-pull
        if self.bound:
            self.properties.pull()
        return key in self.properties

    def __getitem__(self, key):
        # TODO 2.0: remove auto-pull
        if self.bound:
            self.properties.pull()
        return self.properties.__getitem__(key)

    def __setitem__(self, key, value):
        self.properties.__setitem__(key, value)
        # TODO 2.0: remove auto-push
        if self.bound:
            self.properties.push()

    def __delitem__(self, key):
        self.properties.__delitem__(key)
        # TODO 2.0: remove auto-push
        if self.bound:
            self.properties.push()

    @property
    def properties(self):
        """ The set of properties attached to this object.
        """
        return self.__properties

    def bind(self, uri, metadata=None):
        super(PropertyContainer, self).bind(uri, metadata)
        try:
            properties_uri = self.resource.metadata["properties"]
        except KeyError:
            properties_uri = self.resource.metadata["self"] + "/properties"
        self.__properties.bind(properties_uri)

    def unbind(self):
        super(PropertyContainer, self).unbind()
        self.__properties.unbind()

    def pull(self):
        self.resource.get()
        self.__properties.clear()
        properties = self.resource.metadata["data"]
        if properties:
            self.__properties.update(properties)

    def push(self):
        self.__properties.push()

    @deprecated("Use `properties` attribute instead")
    def get_cached_properties(self):
        """ Fetch last known properties without calling the server.

        :return: dictionary of properties
        """
        return self.properties

    @deprecated("Use `pull` method on `properties` attribute instead")
    def get_properties(self):
        """ Fetch all properties.

        :return: dictionary of properties
        """
        if self.bound:
            self.properties.pull()
        return self.properties

    @deprecated("Use `push` method on `properties` attribute instead")
    def set_properties(self, properties):
        """ Replace all properties with those supplied.

        :param properties: dictionary of new properties
        """
        self.properties.clear()
        self.properties.update(properties)
        if self.bound:
            self.properties.push()

    @deprecated("Use `push` method on `properties` attribute instead")
    def delete_properties(self):
        """ Delete all properties.
        """
        self.properties.clear()
        try:
            self.properties.push()
        except UnboundError:
            pass


class Node(PropertyContainer):
    """ A node within a graph, identified by a URI. For example:

        >>> from py2neo import Node
        >>> alice = Node("Person", name="Alice")
        >>> alice
        (:Person {name:"Alice"})

    Typically, concrete nodes will not be constructed directly in this way
    by client applications. Instead, methods such as
    :py:func:`Graph.create` build node objects indirectly as
    required. Once created, nodes can be treated like any other container type
    so as to manage properties::

        # get the `name` property of `node`
        name = node["name"]

        # set the `name` property of `node` to `Alice`
        node["name"] = "Alice"

        # delete the `name` property from `node`
        del node["name"]

        # determine the number of properties within `node`
        count = len(node)

        # determine existence of the `name` property within `node`
        if "name" in node:
            pass

        # iterate through property keys in `node`
        for key in node:
            value = node[key]

    :param uri: URI identifying this node
    """

    @staticmethod
    def cast(*args, **kwargs):
        """ Cast the arguments provided to a :py:class:`neo4j.Node`. The
        following general combinations are possible:

        >>> Node.cast(None)
        >>> Node.cast()
        ()
        >>> Node.cast("Person")
        (:Person)
        >>> Node.cast(name="Alice")
        ({name:"Alice"})
        >>> Node.cast("Person", name="Alice")
        (:Person {name:"Alice"})


        - ``node()``
        - ``node(node_instance)``
        - ``node(property_dict)``
        - ``node(**properties)``
        - ``node(int)`` -> NodePointer(int)
        - ``node(None)`` -> None

        If :py:const:`None` is passed as the only argument, :py:const:`None` is
        returned instead of a ``Node`` instance.

        Examples::

            node()
            node(Node("http://localhost:7474/db/data/node/1"))
            node({"name": "Alice"})
            node(name="Alice")

        Other representations::

            {"name": "Alice"}

        """
        if len(args) == 1 and not kwargs:
            arg = args[0]
            if arg is None:
                return None
            elif isinstance(arg, (Node, NodePointer, BatchRequest)):
                return arg
            elif is_integer(arg):
                return NodePointer(arg)

        inst = Node()

        def apply(x):
            if isinstance(x, dict):
                inst.properties.update(x)
            elif is_collection(x):
                for item in x:
                    apply(item)
            else:
                inst.labels.add(ustr(x))

        for arg in args:
            apply(arg)
        inst.properties.update(kwargs)
        return inst

    @classmethod
    def hydrate(cls, data):
        """ Create a new Node instance from a serialised representation held
        within a dictionary. It is expected there is at least a "self" key
        pointing to a URI for this Node; there may also optionally be
        properties passed in the "data" value.
        """
        self = data["self"]
        properties = data.get("data")
        if properties is None:
            inst = cls()
            inst.bind(self, data)
            inst.__stale = {"labels", "properties"}
        else:
            inst = cls(**properties)
            inst.bind(self, data)
            inst.__stale = {"labels"}
        return inst

    @classmethod
    def join(cls, n, m):
        """ Attempt to combine two equivalent nodes into a single node.
        """

        def is_valid(node):
            return node is None or isinstance(node, (Node, NodePointer, BatchRequest))  # TODO: BatchRequest?

        if not is_valid(n) or not is_valid(m):
            raise TypeError("Can only join Node, NodePointer or None")
        if n is None:
            return m
        elif m is None or n is m:
            return n
        elif isinstance(n, NodePointer) and isinstance(m, NodePointer):
            if n.address == m.address:
                return n
        elif n.bound and m.bound:
            if n.resource == m.resource:
                return n
        raise UnjoinableError("Cannot join nodes {} and {}".format(n, m))

    @classmethod
    @deprecated("Use Node constructor instead")
    def abstract(cls, **properties):
        """ Create and return a new abstract node containing properties drawn
        from the keyword arguments supplied. An abstract node is not bound to
        a concrete node within a database but properties can be managed
        similarly to those within bound nodes::

            >>> alice = Node.abstract(name="Alice")
            >>> alice["name"]
            'Alice'
            >>> alice["age"] = 34
            alice.get_properties()
            {'age': 34, 'name': 'Alice'}

        If more complex property keys are required, abstract nodes may be
        instantiated with the ``**`` syntax::

            >>> alice = Node.abstract(**{"first name": "Alice"})
            >>> alice["first name"]
            'Alice'

        :param properties: node properties
        """
        instance = cls(**properties)
        return instance

    def __init__(self, *labels, **properties):
        PropertyContainer.__init__(self, **properties)
        self.__labels = LabelSet(labels)
        self.__stale = set()

    def __repr__(self):
        r = Representation()
        if self.bound:
            r.write_node(self, "N" + ustr(self._id))
        else:
            r.write_node(self)
        return repr(r)

    def __eq__(self, other):
        if other is None:
            return False
        other = Node.cast(other)
        if self.bound and other.bound:
            return self.resource == other.resource
        elif self.bound or other.bound:
            return False
        else:
            return (LabelSet.__eq__(self.labels, other.labels) and
                    PropertyContainer.__eq__(self, other))

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        if self.bound:
            return hash(self.resource.uri)
        else:
            # TODO: add labels to this hash
            return hash(tuple(sorted(self.properties.items())))

    @property
    def __uri__(self):
        return self.resource.uri

    @property
    def labels(self):
        """ The set of labels attached to this Node.
        """
        if self.bound and "labels" in self.__stale:
            self.pull()
        return self.__labels

    @property
    def properties(self):
        """ The set of properties attached to this Node.
        """
        if self.bound and "properties" in self.__stale:
            self.pull()
        return super(Node, self).properties

    def bind(self, uri, metadata=None):
        super(Node, self).bind(uri, metadata)
        try:
            labels_uri = self.resource.metadata["labels"]
        except KeyError:
            self.__class__ = LegacyNode
        else:
            self.__labels.bind(labels_uri)

    def unbind(self):
        super(Node, self).unbind()
        self.__labels.unbind()

    def pull(self):
        query = CypherQuery(self.graph, "START a=node({a}) RETURN a,labels(a)")
        results = query.execute(a=self._id)
        node, labels = results[0].values
        super(Node, self).properties.clear()
        super(Node, self).properties.update(node.properties)
        self.__labels.clear()
        self.__labels.update(labels)
        self.__stale.clear()

    def push(self):
        # TODO combine this into a single call
        super(Node, self).push()
        self.labels.push()

    @property
    def _id(self):
        """ Return the internal ID for this entity.

        :return: integer ID of this entity within the database.
        """
        return int(self.resource.uri.path.segments[-1])

    @deprecated("Use Graph.delete instead")
    def delete(self):
        """ Delete this entity from the database.
        """
        self.resource.delete()

    @property
    def exists(self):
        """ Detects whether this Node still exists in the database.
        """
        try:
            self.resource.get()
        except ClientError as err:
            if err.status_code == NOT_FOUND:
                return False
            else:
                raise
        else:
            return True

    def delete_related(self):
        """ Delete this node along with all related nodes and relationships.
        """
        if self.graph.supports_foreach_pipe:
            query = ("START a=node({a}) "
                     "MATCH (a)-[rels*0..]-(z) "
                     "FOREACH(r IN rels| DELETE r) "
                     "DELETE a, z")
        else:
            query = ("START a=node({a}) "
                     "MATCH (a)-[rels*0..]-(z) "
                     "FOREACH(r IN rels: DELETE r) "
                     "DELETE a, z")
        CypherQuery(self.graph, query).execute(a=self._id)

    def isolate(self):
        """ Delete all relationships connected to this node, both incoming and
        outgoing.
        """
        CypherQuery(self.graph, "START a=node({a}) "
                                "MATCH a-[r]-b "
                                "DELETE r").execute(a=self._id)

    def match(self, rel_type=None, other_node=None, limit=None):
        """ Iterate through matching relationships attached to this node,
        regardless of direction.

        :param rel_type: type of relationships to match or :py:const:`None` if
            any
        :param other_node: concrete :py:class:`Node` to match for other end of
            relationship or :py:const:`None` if any
        :param limit: maximum number of relationships to match or
            :py:const:`None` if no limit
        :return: matching relationships
        :rtype: generator

        .. seealso::
           :py:func:`Graph.match <py2neo.neo4j.Graph.match>`
        """
        return self.graph.match(self, rel_type, other_node, True, limit)

    def match_incoming(self, rel_type=None, start_node=None, limit=None):
        """ Iterate through matching relationships where this node is the end
        node.

        :param rel_type: type of relationships to match or :py:const:`None` if
            any
        :param start_node: concrete start :py:class:`Node` to match or
            :py:const:`None` if any
        :param limit: maximum number of relationships to match or
            :py:const:`None` if no limit
        :return: matching relationships
        :rtype: generator

        .. seealso::
           :py:func:`Graph.match <py2neo.neo4j.Graph.match>`
        """
        return self.graph.match(start_node, rel_type, self, False, limit)

    def match_outgoing(self, rel_type=None, end_node=None, limit=None):
        """ Iterate through matching relationships where this node is the start
        node.

        :param rel_type: type of relationships to match or :py:const:`None` if
            any
        :param end_node: concrete end :py:class:`Node` to match or
            :py:const:`None` if any
        :param limit: maximum number of relationships to match or
            :py:const:`None` if no limit
        :return: matching relationships
        :rtype: generator

        .. seealso::
           :py:func:`Graph.match <py2neo.neo4j.Graph.match>`
        """
        return self.graph.match(self, rel_type, end_node, False, limit)

    def create_path(self, *items):
        """ Create a new path, starting at this node and chaining together the
        alternating relationships and nodes provided::

            (self)-[rel_0]->(node_0)-[rel_1]->(node_1) ...
                   |-----|  |------| |-----|  |------|
             item:    0        1        2        3

        Each relationship may be specified as one of the following:

        - an existing Relationship instance
        - a string holding the relationship type, e.g. "KNOWS"
        - a (`str`, `dict`) tuple holding both the relationship type and
          its properties, e.g. ("KNOWS", {"since": 1999})

        Nodes can be any of the following:

        - an existing Node instance
        - an integer containing the ID of an existing node
        - a `dict` holding a set of properties for a new node
        - :py:const:`None`, representing an unspecified node that will be
          created as required

        :param items: alternating relationships and nodes
        :return: `Path` object representing the newly-created path
        """
        path = Path(self, *items)
        return path.create(self.graph)

    def get_or_create_path(self, *items):
        """ Identical to `create_path` except will reuse parts of the path
        which already exist.

        Some examples::

            # add dates to calendar, starting at calendar_root
            christmas_day = calendar_root.get_or_create_path(
                "YEAR",  {"number": 2000},
                "MONTH", {"number": 12},
                "DAY",   {"number": 25},
            )
            # `christmas_day` will now contain a `Path` object
            # containing the nodes and relationships used:
            # (CAL)-[:YEAR]->(2000)-[:MONTH]->(12)-[:DAY]->(25)

            # adding a second, overlapping path will reuse
            # nodes and relationships wherever possible
            christmas_eve = calendar_root.get_or_create_path(
                "YEAR",  {"number": 2000},
                "MONTH", {"number": 12},
                "DAY",   {"number": 24},
            )
            # `christmas_eve` will contain the same year and month nodes
            # as `christmas_day` but a different (new) day node:
            # (CAL)-[:YEAR]->(2000)-[:MONTH]->(12)-[:DAY]->(25)
            #                                  |
            #                                [:DAY]
            #                                  |
            #                                  v
            #                                 (24)

        """
        path = Path(self, *items)
        return path.get_or_create(self.graph)

    @deprecated("Use `labels` property instead")
    def get_labels(self):
        """ Fetch all labels associated with this node.

        :return: :py:class:`set` of text labels
        """
        self.labels.pull()
        return self.labels

    @deprecated("Use `add` or `update` method of `labels` property instead")
    def add_labels(self, *labels):
        """ Add one or more labels to this node.

        :param labels: one or more text labels
        """
        labels = [ustr(label) for label in set(flatten(labels))]
        self.labels.update(labels)
        try:
            self.labels.push()
        except ClientError as err:
            if err.status_code == BAD_REQUEST and err.cause.exception == 'ConstraintViolationException':
                raise ValueError(err.cause.message)
            else:
                raise

    @deprecated("Use `remove` method of `labels` property instead")
    def remove_labels(self, *labels):
        """ Remove one or more labels from this node.

        :param labels: one or more text labels
        """
        labels = [ustr(label) for label in set(flatten(labels))]
        batch = WriteBatch(self.graph)
        for label in labels:
            batch.remove_label(self, label)
        batch.run()

    @deprecated("Use `clear` and `update` methods of `labels` property instead")
    def set_labels(self, *labels):
        """ Replace all labels on this node.

        :param labels: one or more text labels
        """
        labels = [ustr(label) for label in set(flatten(labels))]
        self.labels.clear()
        self.add_labels(*labels)


class NodePointer(object):

    def __init__(self, address):
        self.address = address

    def __eq__(self, other):
        return self.address == other.address

    def __ne__(self, other):
        return not self.__eq__(other)


class Rel(PropertyContainer):
    """ A relationship with no start or end nodes.
    """

    @staticmethod
    def cast(*args, **kwargs):
        """ Cast the arguments provided to a Rel object.

        >>> Rel.cast('KNOWS')
        -[:KNOWS]->
        >>> Rel.cast(('KNOWS',))
        -[:KNOWS]->
        >>> Rel.cast('KNOWS', {'since': 1999})
        -[:KNOWS {since:1999}]->
        >>> Rel.cast(('KNOWS', {'since': 1999}))
        -[:KNOWS {since:1999}]->
        >>> Rel.cast('KNOWS', since=1999)
        -[:KNOWS {since:1999}]->

        """

        if len(args) == 1 and not kwargs:
            arg = args[0]
            if arg is None:
                return None
            elif isinstance(arg, (Rel, BatchRequest)):  # TODO: BatchRequest?
                return arg
            elif isinstance(arg, Relationship):
                return arg.rel

        inst = Rel()

        def apply(x):
            if isinstance(x, dict):
                inst.properties.update(x)
            elif is_collection(x):
                for item in x:
                    apply(item)
            else:
                inst.type = ustr(x)

        for arg in args:
            apply(arg)
        inst.properties.update(kwargs)
        return inst

    @classmethod
    def hydrate(cls, data):
        """ Create a new Rel instance from a serialised representation held
        within a dictionary. It is expected there is at least a "self" key
        pointing to a URI for this Rel; there may also optionally be a "type"
        and properties passed in the "data" value.
        """
        type_ = data.get("type")
        properties = data.get("data")
        if properties is None:
            if type_ is None:
                inst = cls()
            else:
                inst = cls(type_)
            inst.bind(data["self"], data)
            inst.__stale = {"properties"}
        else:
            if type_ is None:
                inst = cls(**properties)
            else:
                inst = cls(type_, **properties)
            inst.bind(data["self"], data)
        return inst

    def __init__(self, *type_, **properties):
        if len(type_) > 1:
            raise ValueError("Only one relationship type can be specified")
        PropertyContainer.__init__(self, **properties)
        self.__type = type_[0] if type_ else None
        self.__stale = set()

    def __repr__(self):
        r = Representation()
        if self.bound:
            r.write_rel(self, "R" + ustr(self._id))
        else:
            r.write_rel(self)
        return repr(r)

    def __eq__(self, other):
        return self.type == other.type and self.properties == other.properties

    def __ne__(self, other):
        return not self.__eq__(other)

    def __reversed__(self):
        r = Rev()
        r._Bindable__resource = self._Bindable__resource
        r._PropertyContainer__properties = self._PropertyContainer__properties
        r._Rel__type = self.__type
        r._Rel__stale = self.__stale
        return r

    @property
    def type(self):
        if self.bound and self.__type is None:
            self.pull()
        return self.__type

    @type.setter
    def type(self, name):
        if self.bound:
            raise TypeError("The type of a bound Rel is immutable")
        self.__type = name

    @property
    def properties(self):
        """ The set of properties attached to this Rel.
        """
        if self.bound and "properties" in self.__stale:
            self.pull()
        return super(Rel, self).properties

    def pull(self):
        super(Rel, self).pull()
        self.__type = self.resource.metadata["type"]
        self.__stale.clear()

    @property
    def _id(self):
        """ Return the internal ID for this Rel.

        :return: integer ID of this entity within the database.
        """
        return int(self.resource.uri.path.segments[-1])

    def delete(self):
        """ Delete this Rel from the database.
        """
        self.resource.delete()

    @property
    def exists(self):
        """ Detects whether this Rel still exists in the database.
        """
        try:
            self.resource.get()
        except ClientError as err:
            if err.status_code == NOT_FOUND:
                return False
            else:
                raise
        else:
            return True


class Rev(Rel):

    def __reversed__(self):
        r = Rel()
        r._Bindable__resource = self._Bindable__resource
        r._PropertyContainer__properties = self._PropertyContainer__properties
        r._Rel__type = self._Rel__type
        r._Rel__stale = self._Rel__stale
        return r


class Path(object):
    """ A chain of relationships.

        >>> from py2neo import Node, Path, Rev
        >>> alice, bob, carol = Node(name="Alice"), Node(name="Bob"), Node(name="Carol")
        >>> abc = Path(alice, "KNOWS", bob, Rev("KNOWS"), carol)
        >>> abc
        ({name:"Alice"})-[:KNOWS]->({name:"Bob"})<-[:KNOWS]-({name:"Carol"})
        >>> abc.nodes
        (({name:"Alice"}), ({name:"Bob"}), ({name:"Carol"}))
        >>> abc.rels
        (-[:KNOWS]->, <-[:KNOWS]-)
        >>> abc.relationships
        (({name:"Alice"})-[:KNOWS]->({name:"Bob"}), ({name:"Carol"})-[:KNOWS]->({name:"Bob"}))
        >>> dave, eve = Node(name="Dave"), Node(name="Eve")
        >>> de = Path(dave, "KNOWS", eve)
        >>> de
        ({name:"Dave"})-[:KNOWS]->({name:"Eve"})
        >>> abcde = Path(abc, "KNOWS", de)
        >>> abcde
        ({name:"Alice"})-[:KNOWS]->({name:"Bob"})<-[:KNOWS]-({name:"Carol"})-[:KNOWS]->({name:"Dave"})-[:KNOWS]->({name:"Eve"})

    """

    @classmethod
    def hydrate(cls, data):
        # TODO: fetch directions (Rel/Rev) as they cannot be lazily derived :-(
        nodes = [Node.hydrate({"self": uri}) for uri in data["nodes"]]
        rels = [Rel.hydrate({"self": uri}) for uri in data["relationships"]]
        path = Path(*round_robin(nodes, rels))
        path.__metadata = data
        return path

    def __init__(self, *entities):
        nodes = []
        rels = []

        def join_path(path, index):
            if len(nodes) == len(rels):
                nodes.extend(path.nodes)
                rels.extend(path.rels)
            else:
                # try joining forward
                try:
                    nodes[-1] = Node.join(nodes[-1], path.start_node)
                except UnjoinableError:
                    # try joining backward
                    try:
                        nodes[-1] = Node.join(nodes[-1], path.end_node)
                    except UnjoinableError:
                        raise UnjoinableError("Path at position {} cannot be "
                                              "joined".format(index))
                    else:
                        nodes.extend(path.nodes[-2::-1])
                        rels.extend(reversed(r) for r in path.rels[::-1])
                        # TODO: replace with reverse handler
                else:
                    nodes.extend(path.nodes[1:])
                    rels.extend(path.rels)

        def join_rel(rel, index):
            if len(nodes) == len(rels):
                raise UnjoinableError("Rel at position {} cannot be "
                                      "joined".format(index))
            else:
                rels.append(rel)

        def join_node(node):
            if len(nodes) == len(rels):
                nodes.append(node)
            else:
                nodes[-1] = Node.join(nodes[-1], node)

        for i, entity in enumerate(entities):
            if isinstance(entity, Path):
                join_path(entity, i)
            elif isinstance(entity, Rel):
                join_rel(entity, i)
            elif isinstance(entity, (Node, NodePointer)):
                join_node(entity)
            elif len(nodes) == len(rels):
                join_node(Node.cast(entity))
            else:
                join_rel(Rel.cast(entity), i)
        join_node(None)

        self.__nodes = tuple(nodes)
        self.__rels = tuple(rels)
        self.__relationships = None
        self.__order = len(self.__nodes)
        self.__size = len(self.__rels)
        self.__metadata = None

    def __repr__(self):
        r = Representation()
        r.write_path(self)
        return repr(r)

    def __bool__(self):
        return bool(self.rels)

    def __nonzero__(self):
        return bool(self.rels)

    def __len__(self):
        return self.size

    def __eq__(self, other):
        return self.nodes == other.nodes and self.rels == other.rels

    def __ne__(self, other):
        return not self.__eq__(other)

    def __getitem__(self, item):
        path = Path()
        try:
            if isinstance(item, slice):
                p, q = item.start, item.stop
                if q is not None:
                    q += 1
                path.__nodes = self.nodes[p:q]
                path.__rels = self.rels[item]
            else:
                if item >= 0:
                    path.__nodes = self.nodes[item:item + 2]
                elif item == -1:
                    path.__nodes = self.nodes[-2:None]
                else:
                    path.__nodes = self.nodes[item - 1:item + 1]
                path.__rels = (self.rels[item],)
        except IndexError:
            raise IndexError("Path segment index out of range")
        return path

    @property
    def order(self):
        """ The number of nodes within this path.
        """
        return self.__order

    @property
    def size(self):
        """ The number of relationships within this path.
        """
        return self.__size

    @property
    def start_node(self):
        return self.__nodes[0]

    @property
    def end_node(self):
        return self.__nodes[-1]

    @property
    def nodes(self):
        """ Return a tuple of all the nodes which make up this path.
        """
        return self.__nodes

    @property
    def rels(self):
        """ Return a tuple of all the rels which make up this path.
        """
        return self.__rels

    @property
    def relationships(self):
        """ Return a list of all the relationships which make up this path.
        """
        if self.__relationships is None:
            self.__relationships = tuple(
                Relationship(self.nodes[i], rel, self.nodes[i + 1])
                for i, rel in enumerate(self.rels)
            )
        return self.__relationships

    # TODO: remove - use Path constructor instead
    @classmethod
    def join(cls, left, rel, right):
        """ Join the two paths `left` and `right` with the relationship `rel`.
        """
        if isinstance(left, Path):
            left = left[:]
        else:
            left = Path(left)
        if isinstance(right, Path):
            right = right[:]
        else:
            right = Path(right)
        left.__rels.append(Rel.cast(rel))
        left.__nodes.extend(right.__nodes)
        left.__rels.extend(right.__rels)
        return left

    def _create_query(self, unique):
        nodes, path, values, params = [], [], [], {}

        def append_node(i, node):
            if node is None:
                path.append("(n{0})".format(i))
                values.append("n{0}".format(i))
            elif node.is_abstract:
                path.append("(n{0} {{p{0}}})".format(i))
                params["p{0}".format(i)] = node.properties
                values.append("n{0}".format(i))
            else:
                path.append("(n{0})".format(i))
                nodes.append("n{0}=node({{i{0}}})".format(i))
                params["i{0}".format(i)] = node._id
                values.append("n{0}".format(i))

        def append_rel(i, rel):
            if rel.properties:
                path.append("-[r{0}:`{1}` {{q{0}}}]->".format(i, rel.type))
                params["q{0}".format(i)] = compact(rel.properties)
                values.append("r{0}".format(i))
            else:
                path.append("-[r{0}:`{1}`]->".format(i, rel.type))
                values.append("r{0}".format(i))

        append_node(0, self.__nodes[0])
        for i, rel in enumerate(self.__rels):
            append_rel(i, rel)
            append_node(i + 1, self.__nodes[i + 1])
        clauses = []
        if nodes:
            clauses.append("START {0}".format(",".join(nodes)))
        if unique:
            clauses.append("CREATE UNIQUE p={0}".format("".join(path)))
        else:
            clauses.append("CREATE p={0}".format("".join(path)))
        #clauses.append("RETURN {0}".format(",".join(values)))
        clauses.append("RETURN p")
        query = " ".join(clauses)
        return query, params

    def _create(self, graph, unique):
        query, params = self._create_query(unique=unique)
        try:
            results = CypherQuery(graph, query).execute(**params)
        except CypherError:
            raise NotImplementedError(
                "The Neo4j server at <{0}> does not support "
                "Cypher CREATE UNIQUE clauses or the query contains "
                "an unsupported property type".format(graph.__uri__)
            )
        else:
            for row in results:
                return row[0]

    @deprecated("Use Graph.create(Path(...)) instead")
    def create(self, graph):
        """ Construct a path within the specified `graph` from the nodes
        and relationships within this :py:class:`Path` instance. This makes
        use of Cypher's ``CREATE`` clause.
        """
        return self._create(graph, unique=False)

    @deprecated("Use Graph.merge(Path(...)) instead")
    def get_or_create(self, graph):
        """ Construct a unique path within the specified `graph` from the
        nodes and relationships within this :py:class:`Path` instance. This
        makes use of Cypher's ``CREATE UNIQUE`` clause.
        """
        return self._create(graph, unique=True)

    # service_root/graph/resource
    # bound/bind/unbind
    # pull/push


class Relationship(Path):
    """ A relationship within a graph, identified by a URI.

    :param uri: URI identifying this relationship
    """

    @staticmethod
    def cast(*args, **kwargs):
        """ Cast the arguments provided to a :py:class:`neo4j.Relationship`. The
        following general combinations are possible:

        - ``rel(relationship_instance)``
        - ``rel((start_node, type, end_node))``
        - ``rel((start_node, type, end_node, properties))``
        - ``rel((start_node, (type, properties), end_node))``
        - ``rel(start_node, (type, properties), end_node)``
        - ``rel(start_node, type, end_node, properties)``
        - ``rel(start_node, type, end_node, **properties)``

        Examples::

            rel(Relationship("http://localhost:7474/db/data/relationship/1"))
            rel((alice, "KNOWS", bob))
            rel((alice, "KNOWS", bob, {"since": 1999}))
            rel((alice, ("KNOWS", {"since": 1999}), bob))
            rel(alice, ("KNOWS", {"since": 1999}), bob)
            rel(alice, "KNOWS", bob, {"since": 1999})
            rel(alice, "KNOWS", bob, since=1999)

        Other representations::

            (alice, "KNOWS", bob)
            (alice, "KNOWS", bob, {"since": 1999})
            (alice, ("KNOWS", {"since": 1999}), bob)

        """
        if len(args) == 1 and not kwargs:
            arg = args[0]
            if isinstance(arg, Relationship):
                return arg
            elif isinstance(arg, tuple):
                if len(arg) == 3:
                    return Relationship(*arg)
                elif len(arg) == 4:
                    return Relationship(arg[0], arg[1], arg[2], **arg[3])
                else:
                    raise TypeError("Cannot cast relationship from {0}".format(arg))
            else:
                raise TypeError("Cannot cast relationship from {0}".format(arg))
        elif len(args) == 3:
            rel = Relationship(*args)
            rel.properties.update(kwargs)
            return rel
        elif len(args) == 4:
            props = args[3]
            props.update(kwargs)
            return Relationship(*args[0:3], **props)
        else:
            raise TypeError("Cannot cast relationship from {0}".format((args, kwargs)))

    @classmethod
    def hydrate(cls, data):
        """ Create a new Relationship instance from a serialised representation
        held within a dictionary.
        """
        return cls(Node.hydrate({"self": data["start"]}), Rel.hydrate(data),
                   Node.hydrate({"self": data["end"]}))

    # TODO: remove
    @classmethod
    def abstract(cls, start_node, type_, end_node, **properties):
        """ Create and return a new abstract relationship.
        """
        instance = cls(start_node, type_, end_node, **properties)
        return instance

    def __init__(self, start_node, rel, end_node, **properties):
        cast_rel = Rel.cast(rel)
        if isinstance(cast_rel, Rev):  # always forwards
            Path.__init__(self, end_node, reversed(cast_rel), start_node)
        else:
            Path.__init__(self, start_node, cast_rel, end_node)
        self.rel.properties.update(properties)

    def __repr__(self):
        r = Representation()
        if self.bound:
            r.write_relationship(self, "R" + ustr(self._id))
        else:
            r.write_relationship(self)
        return repr(r)

    def __eq__(self, other):
        if self.bound:
            return self.resource == other.resource
        else:
            return self.nodes == other.nodes and self.rels == other.rels

    def __ne__(self, other):
        return not self.__eq__(other)

    #def __hash__(self):
    #    if self.__uri__:
    #        return hash(self.__uri__)
    #    else:
    #        return hash(tuple(sorted(self._properties.items())))

    @property
    def _id(self):
        return self.rel._id

    @property
    def rel(self):
        return self.rels[0]

    @deprecated("Use Graph.delete instead")
    def delete(self):
        """ Delete this entity from the database.
        """
        self._delete()

    @property
    def exists(self):
        """ Detects whether this entity still exists in the database.
        """
        return self.rel.exists

    @property
    def bound(self):
        return self.rel.bound

    @property
    def resource(self):
        return self.rel.resource

    @property
    def type(self):
        return self.rel.type

    @property
    def properties(self):
        return self.rel.properties

    def update_properties(self, properties):
        """ Update the properties for this relationship with the values
        supplied.
        """
        if self.is_abstract:
            self._properties.update(properties)
            self._properties = compact(self._properties)
        else:
            query, params = ["START a=rel({A})"], {"A": self._id}
            for i, (key, value) in enumerate(properties.items()):
                value_tag = "V" + str(i)
                query.append("SET a.`" + key + "`={" + value_tag + "}")
                params[value_tag] = value
            query.append("RETURN a")
            rel = CypherQuery(self.graph, " ".join(query)).execute_one(**params)
            self._properties = rel.__metadata__["data"]

    # deprecated
    @property
    def is_abstract(self):
        return not self.bound

    def __contains__(self, key):
        return self.rel.__contains__(key)

    def __getitem__(self, key):
        return self.rel.__getitem__(key)

    def __setitem__(self, key, value):
        self.rel.__setitem__(key, value)

    def __delitem__(self, key):
        self.rel.__delitem__(key)

    def __len__(self):
        return self.rel.__len__()

    @property
    def bound(self):
        return self.rel.bound

    def bind(self, uri, metadata=None):
        # TODO: do for start_node, rel and end_node
        self.rel.bind(uri, metadata)

    def unbind(self):
        # TODO: do for start_node, rel and end_node
        self.rel.unbind()

    def pull(self):
        # TODO: do for start_node, rel and end_node
        self.rel.pull()

    def push(self):
        # TODO: do for start_node, rel and end_node
        self.rel.push()

    @property
    def service_root(self):
        try:
            return self.start_node.service_root
        except UnboundError:
            try:
                return self.end_node.service_root
            except UnboundError:
                return self.rel.service_root

    @property
    def graph(self):
        return self.service_root.graph

    @property
    def uri(self):
        return self.rel.uri

    @deprecated("Use `properties` attribute instead")
    def get_cached_properties(self):
        """ Fetch last known properties without calling the server.

        :return: dictionary of properties
        """
        return self.properties

    @deprecated("Use `pull` method on `properties` attribute instead")
    def get_properties(self):
        """ Fetch all properties.

        :return: dictionary of properties
        """
        if self.bound:
            self.properties.pull()
        return self.properties

    @deprecated("Use `push` method on `properties` attribute instead")
    def set_properties(self, properties):
        """ Replace all properties with those supplied.

        :param properties: dictionary of new properties
        """
        self.properties.clear()
        self.properties.update(properties)
        if self.bound:
            self.properties.push()

    @deprecated("Use `push` method on `properties` attribute instead")
    def delete_properties(self):
        """ Delete all properties.
        """
        self.properties.clear()
        try:
            self.properties.push()
        except UnboundError:
            pass


def _cast(obj, cls=(Node, Relationship), abstract=None):
    if obj is None:
        return None
    elif isinstance(obj, Node) or isinstance(obj, dict):
        entity = Node.cast(obj)
    elif isinstance(obj, Relationship) or isinstance(obj, tuple):
        entity = Relationship.cast(obj)
    else:
        raise TypeError(obj)
    if not isinstance(entity, cls):
        raise TypeError(obj)
    if abstract is not None and bool(abstract) != bool(entity.is_abstract):
        raise TypeError(obj)
    return entity


from py2neo.batch import BatchError, BatchRequest, BatchResponse, BatchRequestList, BatchResponseList, ReadBatch, WriteBatch
from py2neo.cypher import CypherError, RecordProducer, Representation
from py2neo.legacy.batch import LegacyReadBatch, LegacyWriteBatch
from py2neo.legacy.index import Index
from py2neo.legacy.neo4j import GraphDatabaseService, LegacyNode
