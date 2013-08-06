#!/usr/bin/env python
#
# Copyright 2007 Google Inc.
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

"""
AppScale modifications 

Distributed Method:
All calls are made to a datastore server for queries, gets, puts, and deletes,
index functions, transaction functions.
"""

import datetime
import logging
import sys
import warnings

from google.appengine.api import api_base_pb
from google.appengine.api import apiproxy_stub
from google.appengine.api import apiproxy_stub_map
from google.appengine.api import datastore
from google.appengine.api import datastore_errors
from google.appengine.api import datastore_types
from google.appengine.api import users
from google.appengine.datastore import datastore_pb
from google.appengine.datastore import datastore_index
from google.appengine.runtime import apiproxy_errors
from google.net.proto import ProtocolBuffer
from google.appengine.datastore import entity_pb
from google.appengine.ext.remote_api import remote_api_pb
from google.appengine.datastore import old_datastore_stub_util

from google.appengine.datastore import googledatastore
from google.appengine.datastore import pb_mapper

# Where the SSL certificate is placed for encrypted communication
CERT_LOCATION = "/etc/appscale/certs/mycert.pem"

# Where the SSL private key is placed for encrypted communication
KEY_LOCATION = "/etc/appscale/certs/mykey.pem"

# The default SSL port to connect to
SSL_DEFAULT_PORT = 8443

# The amount of time before we consider a query cursor to be no longer valid.
CURSOR_TIMEOUT = 120

try:
  __import__('google.appengine.api.taskqueue.taskqueue_service_pb')
  taskqueue_service_pb = sys.modules.get(
      'google.appengine.api.taskqueue.taskqueue_service_pb')
except ImportError:
  from google.appengine.api.taskqueue import taskqueue_service_pb

warnings.filterwarnings('ignore', 'tempnam is a potential security risk')


entity_pb.Reference.__hash__ = lambda self: hash(self.Encode())
datastore_pb.Query.__hash__ = lambda self: hash(self.Encode())
datastore_pb.Transaction.__hash__ = lambda self: hash(self.Encode())


_MAX_QUERY_COMPONENTS = 100


_BATCH_SIZE = 20


_MAX_ACTIONS_PER_TXN = 5

# These application IDs use the AppScale backend, rather than the
# Google Cloud Datastore backend.
_RESERVED_APP_IDS = ["appscaledashboard", "apichecker"]

# Google service account for Google Cloud Datastore.
DEFAULT_SERVICE_ACCOUNT = "399068749927-11atpdlu60fv73ip8jnhvftr3kt7kd8l@developer.gserviceaccount.com"

# Google private key for Google Cloud Datastore.
DEFAULT_PRIVATE_KEY_FILE = "/root/2574a2a5f891af1afb67c13de3be28648a46833f-privatekey.p12"

# Default test GCD dataset.
DEFAULT_DATASET = "quick-asset-252"

class DatastoreDistributed(apiproxy_stub.APIProxyStub):
  """ A central server hooks up to a db and communicates via protocol 
      buffers.

  """

  _PROPERTY_TYPE_TAGS = {
    datastore_types.Blob: entity_pb.PropertyValue.kstringValue,
    bool: entity_pb.PropertyValue.kbooleanValue,
    datastore_types.Category: entity_pb.PropertyValue.kstringValue,
    datetime.datetime: entity_pb.PropertyValue.kint64Value,
    datastore_types.Email: entity_pb.PropertyValue.kstringValue,
    float: entity_pb.PropertyValue.kdoubleValue,
    datastore_types.GeoPt: entity_pb.PropertyValue.kPointValueGroup,
    datastore_types.IM: entity_pb.PropertyValue.kstringValue,
    int: entity_pb.PropertyValue.kint64Value,
    datastore_types.Key: entity_pb.PropertyValue.kReferenceValueGroup,
    datastore_types.Link: entity_pb.PropertyValue.kstringValue,
    long: entity_pb.PropertyValue.kint64Value,
    datastore_types.PhoneNumber: entity_pb.PropertyValue.kstringValue,
    datastore_types.PostalAddress: entity_pb.PropertyValue.kstringValue,
    datastore_types.Rating: entity_pb.PropertyValue.kint64Value,
    str: entity_pb.PropertyValue.kstringValue,
    datastore_types.Text: entity_pb.PropertyValue.kstringValue,
    type(None): 0,
    unicode: entity_pb.PropertyValue.kstringValue,
    users.User: entity_pb.PropertyValue.kUserValueGroup,
    }

  WRITE_ONLY = entity_pb.CompositeIndex.WRITE_ONLY
  READ_WRITE = entity_pb.CompositeIndex.READ_WRITE
  DELETED = entity_pb.CompositeIndex.DELETED
  ERROR = entity_pb.CompositeIndex.ERROR

  _INDEX_STATE_TRANSITIONS = {
    WRITE_ONLY: frozenset((READ_WRITE, DELETED, ERROR)),
    READ_WRITE: frozenset((DELETED,)),
    ERROR: frozenset((DELETED,)),
    DELETED: frozenset((ERROR,)),
  }

  def __init__(self,
               app_id,
               datastore_location,
               history_file=None,
               require_indexes=False,
               service_name='datastore_v3',
               trusted=False,
               private_key=DEFAULT_PRIVATE_KEY_FILE,
               email_account=DEFAULT_SERVICE_ACCOUNT,
               dataset=DEFAULT_DATASET):
    """Constructor.

    Args:
      app_id: string
      datastore_location: location of datastore server
      history_file: DEPRECATED. No-op.
      require_indexes: bool, default False.  If True, composite indexes must
          exist in index.yaml for queries that need them.
      service_name: Service name expected for all calls.
      trusted: bool, default False.  If True, this stub allows an app to
        access the data of another app.
      private_key: A str, the Google Cloud Datastore private key location.
      email_account: A str, the Google Cloud Datastore email account.
      dataset: A str, the Google Cloud Datastore dataset.
    """
    super(DatastoreDistributed, self).__init__(service_name)

    # TODO lock any use of these global variables
    assert isinstance(app_id, basestring) and app_id != ''
    self.__app_id = app_id
    self.__private_key = private_key
    self.__email_account = email_account
    self.__dataset = dataset
    self.__datastore_location = datastore_location

    self.__is_encrypted = True
    res = self.__datastore_location.split(':')
    if len(res) == 2:
      if int(res[1]) != SSL_DEFAULT_PORT:
        self.__is_encrypted = False

    self.SetTrusted(trusted)

    self.__entities = {}

    self.__schema_cache = {}

    self.__tx_actions_dict = {}
    self.__tx_actions = set()

    self.__queries = {}

    self.__require_indexes = require_indexes


    self.__txn_requests = {}

    # A tuple of datetime and a list of entities from put requests.
    self.__txn_put_request = {}

    # A tuple of datetime and a list of keys from delete requests.
    self.__txn_delete_request = {}

    self.__mapper = pb_mapper.PbMapper(app_id=self.__app_id, 
      dataset=self.__dataset, service_email=self.__email_account, 
      private_key=self.__private_key)

  def Clear(self):
    """ Clears the datastore by deleting all currently stored entities and
    queries. """
    self.__entities = {}
    self.__queries = {}
    self.__schema_cache = {}
    self.__txn_requests = {}

  def SetTrusted(self, trusted):
    """Set/clear the trusted bit in the stub.

    This bit indicates that the app calling the stub is trusted. A
    trusted app can write to datastores of other apps.

    Args:
      trusted: boolean.
    """
    self.__trusted = trusted

  def __ValidateAppId(self, app_id):
    """Verify that this is the stub for app_id.

    Args:
      app_id: An application ID.

    Raises:
      datastore_errors.BadRequestError: if this is not the stub for app_id.
    """
    assert app_id
    if not self.__trusted and app_id != self.__app_id:
      raise datastore_errors.BadRequestError(
          'app %s cannot access app %s\'s data' % (self.__app_id, app_id))

  def __ValidateKey(self, key):
    """Validate this key.

    Args:
      key: entity_pb.Reference

    Raises:
      datastore_errors.BadRequestError: if the key is invalid
    """
    assert isinstance(key, entity_pb.Reference)

    self.__ValidateAppId(key.app())

    for elem in key.path().element_list():
      if elem.has_id() == elem.has_name():
        raise datastore_errors.BadRequestError(
          'each key path element should have id or name but not both: %r' % key)

  def _AppIdNamespaceKindForKey(self, key):
    """ Get (app, kind) tuple from given key.

    The (app, kind) tuple is used as an index into several internal
    dictionaries, e.g. __entities.

    Args:
      key: entity_pb.Reference

    Returns:
      Tuple (app, kind), both are unicode strings.
    """
    last_path = key.path().element_list()[-1]
    return (datastore_types.EncodeAppIdNamespace(key.app(), key.name_space()),
        last_path.type())

  READ_PB_EXCEPTIONS = (ProtocolBuffer.ProtocolBufferDecodeError, LookupError,
                        TypeError, ValueError)
  READ_ERROR_MSG = ('Data in %s is corrupt or a different version. '
                    'Try running with the --clear_datastore flag.\n%r')
  READ_PY250_MSG = ('Are you using FloatProperty and/or GeoPtProperty? '
                    'Unfortunately loading float values from the datastore '
                    'file does not work with Python 2.5.0. '
                    'Please upgrade to a newer Python 2.5 release or use '
                    'the --clear_datastore flag.\n')

  def Read(self):
    """ Does Nothing    """
    return
  def Write(self):
    """ Does Nothing   """
    return 

  def MakeSyncCall(self, service, call, request, response):
    """ The main RPC entry point. service must be 'datastore_v3'.
    """
    self.assertPbIsInitialized(request)
    super(DatastoreDistributed, self).MakeSyncCall(service,
                                                call,
                                                request,
                                                response)
    self.assertPbIsInitialized(response)

  def assertPbIsInitialized(self, pb):
    """Raises an exception if the given PB is not initialized and valid."""
    explanation = []
    assert pb.IsInitialized(explanation), explanation
    pb.Encode()

  def QueryHistory(self):
    """Returns a dict that maps Query PBs to times they've been run."""
    return []

  def _RemoteSend(self, request, response, method):
    """Sends a request remotely to the datstore server. """
    tag = self.__app_id
    user = users.GetCurrentUser()
    if user != None:
      tag += ":" + user.email()
      tag += ":" + user.nickname()
      tag += ":" + user.auth_domain()
    api_request = remote_api_pb.Request()
    api_request.set_method(method)
    api_request.set_service_name("datastore_v3")
    api_request.set_request(request.Encode())

    api_response = remote_api_pb.Response()
    api_response = api_request.sendCommand(self.__datastore_location,
      tag,
      api_response,
      1,
      self.__is_encrypted, 
      KEY_LOCATION,
      CERT_LOCATION)

    if not api_response or not api_response.has_response():
      raise datastore_errors.InternalError(
          'No response from db server on %s requests.' % method)
    
    if api_response.has_application_error():
      error_pb = api_response.application_error()
      logging.error(error_pb.detail())
      raise apiproxy_errors.ApplicationError(error_pb.code(),
                                             error_pb.detail())

    if api_response.has_exception():
      raise api_response.exception()
   
    response.ParseFromString(api_response.response())

  def _assign_ids(self, entities):
    """ Assigns IDs for a list of entities which lack IDs. 
 
    Args:
      entities: A list of entitiy_pb.EntityProto.
    Returns:
      A list of entity_pb.References which have full key paths assigned.
    """
    full_path_keys = []
    requires_ids = False
    for entity in entities:
      last_path = entity.key().path().element_list()[-1]
      if last_path.id() == 0 and not last_path.has_name():
        requires_ids = True
        allocate_id_req = googledatastore.AllocateIdsRequest()
        new_key = allocate_id_req.key.add() 
        for element in entity.key().path().element_list():
          path_element = new_key.path_element.add()
          path_element.kind = element.type()
          if element.has_name():
            path_element.name = element.name()
          elif element.id() != 0:
            path_element.id = element.id()
      else:
        full_path_keys.append(entity.key())

    if not requires_ids:
      return full_path_keys

    # Get full paths from GCD. 
    allocate_resp = googledatastore.AllocateIds(allocate_id_req)
    for key in allocate_resp.key:
      new_key = entity_pb.Reference() 
      new_key.set_app(self.__app_id)
      for path_element in key.path_element:
        new_element = new_key.mutable_path().add_element()
        new_element.set_id(path_element.id)
        new_element.set_type(path_element.kind)
      full_path_keys.append(new_key)

    return full_path_keys
   
  def _check_handle(self, handle):
    """ Checks to see if a transaction handle exists.
 
    Args:
      handle: The transaction identifier.
    Returns:
      True if the transaction exists, False otherwise.
    """
    return txn_handle in self.__txn_requests
 
  def _Dynamic_Put(self, put_request, put_response):
    """Send a put request to the datastore server. """
    if self.__app_id in _RESERVED_APP_IDS:
      put_request.set_trusted(self.__trusted)
      self._RemoteSend(put_request, put_response, "Put")
      return put_response 

    put_request.set_trusted(self.__trusted)

    # Any puts which are not in a transaction will have a transaction 
    # wrapped around it to ensure transaction semantics.
    create_transaction_wrapper = False
    txn_handle = None
    txn_res = datastore_pb.Transaction()
    txn_req = datastore_pb.BeginTransactionRequest()
    if not put_request.has_transaction():
      create_transaction_wrapper = True
      txn_req.set_app(self.__app_id)
      self._Dynamic_BeginTransaction(txn_req, txn_res)
      txn_handle = txn_res.handle()
    else:
      txn_handle = put_request.transaction().handle()    

    if not self._check_handle(txn_handle):
      raise Exception("Transaction %s does not exist" % txn_handle)

    entity_list = put_request.entity_list()
    if txn_handle in self.__txn_put_request:
      self.__txn_put_request[txn_handle].extend(entity_list)
    else:
      self.__txn_put_request[txn_handle] = entity_list

    # Acquire IDs for puts which do not have ids and get the key list which
    # has full key paths.
    key_list = self._assign_ids(entity_list)
    put_response.key_list().extend(key_list)

    # Commit this wrapped transaction if its a stand alone put.
    if create_transaction_wrapper:
      self._Dynamic_Commit(txn_res, api_base_pb.VoidProto())

    return put_response

  def _Dynamic_Get(self, get_request, get_response):
    """Send a get request to the datastore server. """
    if self.__app_id in _RESERVED_APP_IDS:
      self._RemoteSend(get_request, get_response, "Get")
    else:
      req = self.__mapper.convert_get_request(get_request)
      response = self.__mapper.send_lookup(req)
      self.__mapper.convert_get_response(response, get_response)

    return get_response


  def _Dynamic_Delete(self, delete_request, delete_response):
    """Send a delete request to the datastore server. """
    if self.__app_id in _RESERVED_APP_IDS:
      delete_request.set_trusted(self.__trusted)
      self._RemoteSend(delete_request, delete_response, "Delete")
      return delete_response

    delete_request.set_trusted(self.__trusted)

    # Any deletes which are not in a transaction will have a transaction 
    # wrapped around it to ensure transaction semantics.
    create_transaction_wrapper = False
    txn_req = datastore_pb.BeginTransactionRequest()
    txn_res = datastore_pb.Transaction()
    txn_handle = None
    if not delete_request.has_transaction():
      create_transaction_wrapper = True
      txn_req.set_app(self.__app_id)
      self._Dynamic_BeginTransaction(txn_req, txn_res)
      txn_handle = txn_res.handle()
    else:
      txn_handle = delete_request.transaction().handle()    

    if not self._check_handle():
      raise Exception("Transaction %s does not exist" % txn_handle)
    
    key_list = delete_request.key_list()
    if txn_handle in self.__txn_delete_request:
      self.__txn_delete_request[txn_handle].extend(key_list)
    else:
      self.__txn_delete_request[txn_handle] = key_list

    # Commit this wrapped transaction if its a stand alone delete.
    if create_transaction_wrapper:
      self._Dynamic_Commit(txn_res, api_base_pb.VoidProto())

    return delete_response

  def __cleanup_old_cursors(self):
    """ Remove any cursors which are no longer being used. """
    for key in self.__queries.keys():
      _, time_stamp = self.__queries[key]
      # This calculates the time in the future when this cursor is no longer 
      # valid.
      timeout_time = time_stamp + datetime.timedelta(seconds=CURSOR_TIMEOUT)
      if datetime.datetime.now() > timeout_time:
        del self.__queries[key]

  def _Dynamic_RunQuery(self, query, query_result):
    """Send a query request to the datastore server. """

    if query.has_transaction():
      if not query.has_ancestor():
        raise apiproxy_errors.ApplicationError(
          datastore_pb.Error.BAD_REQUEST,
          'Only ancestor queries are allowed inside transactions.')

    (filters, orders) = datastore_index.Normalize(query.filter_list(),
                                                  query.order_list(), [])
    
    old_datastore_stub_util.FillUsersInQuery(filters)

    query_response = datastore_pb.QueryResult()
    if not query.has_app():
      query.set_app(self.__app_id)

    self.__ValidateAppId(query.app())

    if query.app() not in _RESERVED_APP_IDS:
      req = self.__mapper.convert_query_request(query)
      response = self.__mapper.send_query(req)
      self.__mapper.convert_query_response(response, query_response)
    else:
      self._RemoteSend(query, query_response, "RunQuery")

    skipped_results = 0
    if query_response.has_skipped_results():
      skipped_results = query_response.skipped_results()

    def has_prop_indexed(entity, prop):
      """Returns True if prop is in the entity and is indexed."""
      if prop in datastore_types._SPECIAL_PROPERTIES:
        return True
      elif prop in entity.unindexed_properties():
        return False

      values = entity.get(prop, [])
      if not isinstance(values, (tuple, list)):
        values = [values]

      for value in values:
        if type(value) not in datastore_types._RAW_PROPERTY_TYPES:
          return True
      return False

    def order_compare_entities(a, b):
      """ Return a negative, zero or positive number depending on whether
      entity a is considered smaller than, equal to, or larger than b,
      according to the query's orderings. """
      cmped = 0
      for o in orders:
        prop = o.property().decode('utf-8')

        reverse = (o.direction() is datastore_pb.Query_Order.DESCENDING)

        a_val = datastore._GetPropertyValue(a, prop)
        if isinstance(a_val, list):
          a_val = sorted(a_val, order_compare_properties, reverse=reverse)[0]

        b_val = datastore._GetPropertyValue(b, prop)
        if isinstance(b_val, list):
          b_val = sorted(b_val, order_compare_properties, reverse=reverse)[0]

        cmped = order_compare_properties(a_val, b_val)

        if o.direction() is datastore_pb.Query_Order.DESCENDING:
          cmped = -cmped

        if cmped != 0:
          return cmped

      if cmped == 0:
        return cmp(a.key(), b.key())

    def order_compare_entities_pb(a, b):
      """ Return a negative, zero or positive number depending on whether
      entity a is considered smaller than, equal to, or larger than b,
      according to the query's orderings. a and b are protobuf-encoded
      entities."""
      return order_compare_entities(datastore.Entity.FromPb(a),
                                    datastore.Entity.FromPb(b))

    def order_compare_properties(x, y):
      """Return a negative, zero or positive number depending on whether
      property value x is considered smaller than, equal to, or larger than
      property value y. If x and y are different types, they're compared based
      on the type ordering used in the real datastore, which is based on the
      tag numbers in the PropertyValue PB.
      """
      if isinstance(x, datetime.datetime):
        x = datastore_types.DatetimeToTimestamp(x)
      if isinstance(y, datetime.datetime):
        y = datastore_types.DatetimeToTimestamp(y)

      x_type = self._PROPERTY_TYPE_TAGS.get(x.__class__)
      y_type = self._PROPERTY_TYPE_TAGS.get(y.__class__)

      if x_type == y_type:
        try:
          return cmp(x, y)
        except TypeError:
          return 0
      else:
        return cmp(x_type, y_type)

    results = query_response.result_list()
    results = [datastore.Entity._FromPb(r) for r in results]
    results = [r._ToPb() for r in results]
    for result in results:
      old_datastore_stub_util.PrepareSpecialPropertiesForLoad(result)

    old_datastore_stub_util.ValidateQuery(query, filters, orders,
          _MAX_QUERY_COMPONENTS)

    cursor = old_datastore_stub_util.ListCursor(query, results,
                                            order_compare_entities_pb)
    self.__cleanup_old_cursors() 
    self.__queries[cursor.cursor] = cursor, datetime.datetime.now()

    if query.has_count():
      count = query.count()
    elif query.has_limit():
      count = query.limit()
    else:
      count = _BATCH_SIZE

    cursor.PopulateQueryResult(query_result, count,
                               query.offset(), compile=query.compile())
    query_result.set_skipped_results(skipped_results)
    if query.compile():
      compiled_query = query_result.mutable_compiled_query()
      compiled_query.set_keys_only(query.keys_only())
      compiled_query.mutable_primaryscan().set_index_name(query.Encode())

  def _Dynamic_Next(self, next_request, query_result):
    """Get the next set of entities from a previously run query. """
    self.__ValidateAppId(next_request.cursor().app())

    cursor_handle = next_request.cursor().cursor()
    if cursor_handle not in self.__queries:
      raise apiproxy_errors.ApplicationError(
            datastore_pb.Error.BAD_REQUEST, 
            'Cursor %d not found' % cursor_handle)
 
    cursor, _ = self.__queries[cursor_handle]
    if cursor.cursor != cursor_handle:
      raise apiproxy_errors.ApplicationError(
            datastore_pb.Error.BAD_REQUEST, 
            'Cursor %d not found' % cursor_handle)

    assert cursor.app == next_request.cursor().app()
    count = _BATCH_SIZE
    if next_request.has_count():
      count = next_request.count()
    cursor.PopulateQueryResult(query_result, count,
                               next_request.offset(),
                               next_request.compile())

  def _Dynamic_Count(self, query, integer64proto):
    """Get the number of entities for a query. """
    query_result = datastore_pb.QueryResult()
    self._Dynamic_RunQuery(query, query_result)
    count = query_result.result_size()
    integer64proto.set_value(count)

  def _Dynamic_BeginTransaction(self, request, transaction):
    """Send a begin transaction request from the datastore server. """
    if self.__app_id in _RESERVED_APP_IDS:
      request.set_app(self.__app_id)
      self._RemoteSend(request, transaction, "BeginTransaction")
      self.__tx_actions[transaction.handle()] = []
      return transaction

    req = self.__mapper.convert_begin_transaction_request(request)
    response = self.__mapper.send_begin_transaction_request(req)

    if response.handle() in self.__txn_requests:
      raise apiproxy_errors.ApplicationError(
        datastore_pb.Error.BAD_REQUEST,
        "Transaction %s already exists" % response.handle())

    self.__txn_requests[response.handle()] = datetime.datetime.now()

    transaction.MergeFrom(self.__mapper.convert_begin_transaction_response(
      response))
    return transaction

  def _Dynamic_AddActions(self, request, _):
    """Associates the creation of one or more tasks with a transaction.

    Args:
      request: A taskqueue_service_pb.TaskQueueBulkAddRequest containing the
          tasks that should be created when the transaction is comitted.
    """
    # TODO make all tx actions apart of a transaction handle.
    if ((len(self.__tx_actions) + request.add_request_size()) >
        _MAX_ACTIONS_PER_TXN):
      raise apiproxy_errors.ApplicationError(
          datastore_pb.Error.BAD_REQUEST,
          'Too many messages, maximum allowed %s' % _MAX_ACTIONS_PER_TXN)

    new_actions = []
    for add_request in request.add_request_list():
      clone = taskqueue_service_pb.TaskQueueAddRequest()
      clone.CopyFrom(add_request)
      clone.clear_transaction()
      new_actions.append(clone)

    self.__tx_actions.extend(new_actions)

  def _do_transaction_actions(self, txn_handle):
    """ Runs the transaction items that were added for a transaction.
 
    Args:
      txn_handle: An int, the transaction handle.
    """
    response = taskqueue_service_pb.TaskQueueAddResponse()
    try:
      if txn_handle in self.__tx_actions:
        for action in self.__tx_actions[txn_handle]:
          try:
            apiproxy_stub_map.MakeSyncCall(
                'taskqueue', 'Add', action, response)
          except apiproxy_errors.ApplicationError, e:
            logging.warning('Transactional task %s has been dropped, %s',
                            action, e)
    finally:
      if txn_handle in self.__tx_actions:
        del self.__tx_actions[txn_handle]

  def _Dynamic_Commit(self, transaction, transaction_response):
    """ Send a transaction request to commit a transaction to the 
        datastore server. """
    transaction.set_app(self.__app_id)

    if self.__app_id in _RESERVED_APP_IDS:
      self._RemoteSend(transaction, transaction_response, "Commit")
      self._do_transaction_actions(transaction.handle()) 
      return

    handle = transaction.handle()
    if not self._check_handle(handle):
      raise apiproxy_errors.ApplicationError(
        datastore_pb.Error.BAD_REQUEST,
        "Transaction %s does not exist" % handle)

    puts = []
    deletes = []
    if handle in self.__txn_put_request:
      puts = self.__txn_put_request[handle]
    if handle in self.__txn_delete_request:
      delets = self.__txn_delete_request[handle]

    commit_req = self.__mapper.create_commit_request(transaction, puts, 
      deletes)
    self.__mapper.send_commit(commit_req)

    self._do_transaction_actions(transaction.handle()) 

    del self.__txn_requests[handle]

    if handle in self.__txn_put_request:
      del self.__txn_put_request[handle]
    if handle in self.__txn_delete_request:
      del self.__txn_delete_request[handle]
  
  def _Dynamic_Rollback(self, transaction, transaction_response):
    """ Send a rollback request to the datastore server. """
    transaction.set_app(self.__app_id)
 
    if transaction.handle() in self.__tx_actions:
      del self.__tx_actions[transaction.handle()]
    if handle in self.__txn_put_request:
      del self.__txn_put_request[handle]
    if handle in self.__txn_delete_request:
      del self.__txn_delete_request[handle]

    if self.__app_id in _RESERVED_APP_IDS:
      self._RemoteSend(transaction, transaction_response, "Rollback")
    else:
      rollback = googledatastore.RollbackRequest()
      rollback.transaction = transaction.handle()
      googledatastore.rollback(rollback)

    return transaction_response

  def _Dynamic_GetSchema(self, req, schema):
    """ Get the schema of a particular kind of entity. """
    app_str = req.app()
    self.__ValidateAppId(app_str)

    namespace_str = req.name_space()
    app_namespace_str = datastore_types.EncodeAppIdNamespace(app_str,
                                                             namespace_str)
    kinds = []

    for app_namespace, kind in self.__entities:
      if (app_namespace != app_namespace_str or
          (req.has_start_kind() and kind < req.start_kind()) or
          (req.has_end_kind() and kind > req.end_kind())):
        continue

      app_kind = (app_namespace_str, kind)
      if app_kind in self.__schema_cache:
        kinds.append(self.__schema_cache[app_kind])
        continue

      kind_pb = entity_pb.EntityProto()
      kind_pb.mutable_key().set_app('')
      kind_pb.mutable_key().mutable_path().add_element().set_type(kind)
      kind_pb.mutable_entity_group()

      props = {}

      for entity in self.__entities[app_kind].values():
        for prop in entity.protobuf.property_list():
          if prop.name() not in props:
            props[prop.name()] = entity_pb.PropertyValue()
          props[prop.name()].MergeFrom(prop.value())

      for value_pb in props.values():
        if value_pb.has_int64value():
          value_pb.set_int64value(0)
        if value_pb.has_booleanvalue():
          value_pb.set_booleanvalue(False)
        if value_pb.has_stringvalue():
          value_pb.set_stringvalue('none')
        if value_pb.has_doublevalue():
          value_pb.set_doublevalue(0.0)
        if value_pb.has_pointvalue():
          value_pb.mutable_pointvalue().set_x(0.0)
          value_pb.mutable_pointvalue().set_y(0.0)
        if value_pb.has_uservalue():
          value_pb.mutable_uservalue().set_gaiaid(0)
          value_pb.mutable_uservalue().set_email('none')
          value_pb.mutable_uservalue().set_auth_domain('none')
          value_pb.mutable_uservalue().clear_nickname()
          value_pb.mutable_uservalue().clear_obfuscated_gaiaid()
        if value_pb.has_referencevalue():
          value_pb.clear_referencevalue()
          value_pb.mutable_referencevalue().set_app('none')
          pathelem = value_pb.mutable_referencevalue().add_pathelement()
          pathelem.set_type('none')
          pathelem.set_name('none')

      for name, value_pb in props.items():
        prop_pb = kind_pb.add_property()
        prop_pb.set_name(name)
        prop_pb.set_multiple(False)
        prop_pb.mutable_value().CopyFrom(value_pb)

      kinds.append(kind_pb)
      self.__schema_cache[app_kind] = kind_pb

    for kind_pb in kinds:
      kind = schema.add_kind()
      kind.CopyFrom(kind_pb)
      if not req.properties():
        kind.clear_property()

    schema.set_more_results(False)

  def _Dynamic_AllocateIds(self, allocate_ids_request, allocate_ids_response):
    """Send a request for allocation of IDs to the datastore server. """
    self._RemoteSend(allocate_ids_request, allocate_ids_response, "AllocateIds")
    return  allocate_ids_response

  def _Dynamic_CreateIndex(self, index, id_response):
    """ Create a new index. Currently stubbed out."""
    self.__ValidateAppId(index.app_id())
    if index.id() != 0:
      raise apiproxy_errors.ApplicationError(datastore_pb.Error.BAD_REQUEST,
                                             'New index id must be 0.')
    id_response.set_value(0)
    return id_response

  def _Dynamic_GetIndices(self, app_str, composite_indices):
    """ Gets the indices of the current app. Currently stubbed out. """
    return 

  def _Dynamic_UpdateIndex(self, index, void):
    """ Updates the indices of the current app. Currently stubbed out. """
    return 
    
  def _Dynamic_DeleteIndex(self, index, void):
    """ Deletes an index of the current app. Currently stubbed out. """
    return void
