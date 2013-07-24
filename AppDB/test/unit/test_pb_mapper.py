#!/usr/bin/env python
# Programmer: Navraj Chohan <raj@appscale.com>

import os
import sys
import unittest

import googledatastore
from flexmock import flexmock

sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))  
import pb_mapper

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../AppServer"))  
from google.appengine.datastore import datastore_pb
from google.appengine.datastore import entity_pb
from google.appengine.ext import db


class TestPBMapper(unittest.TestCase):
  """
  A set of test cases for the GAE datastore protocol buffers to Google 
  Cloud Datastore protocol buffers, and vice versa.
  """
  def test_init(self):
    googledatastore = flexmock()
    pb_mapper.PbMapper(app_id='app_id', dataset='dataset') 

  def test_get_property_value(self):
    fake_property = flexmock()
    fake_property.should_receive("has_value").and_return(False)
    mapper = pb_mapper.PbMapper(app_id='app_id', dataset='dataset') 
    self.assertEquals((None, None), mapper.get_property_value(fake_property))

    # Test dates.
    fake_value = flexmock()
    fake_value.should_receive("has_int64value").and_return(True)
    fake_value.should_receive("int64value").and_return(123)

    fake_property.should_receive("has_value").and_return(True)
    fake_property.should_receive("value").and_return(fake_value)
    fake_property.should_receive("has_meaning").and_return(True)
    fake_property.should_receive("meaning").and_return(entity_pb.Property.GD_WHEN)
    self.assertEquals((pb_mapper.ValueType.DATE, 123),
       mapper.get_property_value(fake_property))

    # Test ints.
    fake_property.should_receive("has_meaning").and_return(False)
    self.assertEquals((pb_mapper.ValueType.INT, 123),
       mapper.get_property_value(fake_property))
 
    # Test booleans. 
    fake_value.should_receive("has_int64value").and_return(False)
    fake_value.should_receive("has_booleanvalue").and_return(True)
    fake_value.should_receive("booleanvalue").and_return(True)
    self.assertEquals((pb_mapper.ValueType.BOOL, True),
       mapper.get_property_value(fake_property))
   
    # Test doubles. 
    fake_value.should_receive("has_booleanvalue").and_return(False)
    fake_value.should_receive("has_doublevalue").and_return(True)
    fake_value.should_receive("doublevalue").and_return(321)
    self.assertEquals((pb_mapper.ValueType.DOUBLE, 321),
       mapper.get_property_value(fake_property))

    # Test all different type of strings.
    fake_value.should_receive("has_doublevalue").and_return(False)
    fake_value.should_receive("has_stringvalue").and_return(True)
    fake_value.should_receive("stringvalue").and_return("somestring")
    fake_property.should_receive("has_meaning").and_return(True)
    fake_property.should_receive("meaning").and_return(entity_pb.Property.BLOBKEY)
    self.assertEquals((pb_mapper.ValueType.BLOB_KEY, "somestring"),
       mapper.get_property_value(fake_property))
    
    fake_property.should_receive("meaning").and_return(entity_pb.Property.TEXT)
    self.assertEquals((pb_mapper.ValueType.TEXT, "somestring"),
       mapper.get_property_value(fake_property))

    fake_property.should_receive("meaning").and_return(entity_pb.Property.BYTESTRING)
    self.assertEquals((pb_mapper.ValueType.BLOB_STRING, "somestring"),
       mapper.get_property_value(fake_property))

    fake_property.should_receive("has_meaning").and_return(False)
    self.assertEquals((pb_mapper.ValueType.STRING, "somestring"),
       mapper.get_property_value(fake_property))
 
    # Test GeoPoints.
    fake_value.should_receive("has_stringvalue").and_return(False)
    fake_value.should_receive("has_pointvalue").and_return(True)
    fake_value.should_receive("pointvalue").and_return("point")
    self.assertEquals((pb_mapper.ValueType.POINT, "point"),
       mapper.get_property_value(fake_property))

    # Test users.
    fake_value.should_receive("has_pointvalue").and_return(False)
    fake_value.should_receive("has_uservalue").and_return(True)
    fake_value.should_receive("uservalue").and_return("user")
    self.assertEquals((pb_mapper.ValueType.USER, "user"),
       mapper.get_property_value(fake_property))

    # Test references.
    fake_value.should_receive("has_uservalue").and_return(False)
    fake_value.should_receive("has_referencevalue").and_return(True)
    fake_value.should_receive("referencevalue").and_return("reference")
    self.assertEquals((pb_mapper.ValueType.REFERENCE, "reference"),
       mapper.get_property_value(fake_property))

  def test_send_blind_write(self):
    flexmock(googledatastore).should_receive("blind_write").and_return("test")
    mapper = pb_mapper.PbMapper(app_id='app_id', dataset='dataset') 
    fake_request = flexmock()
    self.assertEquals("test", mapper.send_blind_write(fake_request))

  def test_set_properties(self):
    mapper = pb_mapper.PbMapper(app_id='app_id', dataset='dataset') 
    # Test empty args.
    mapper.set_properties(None, [])
   
    # Test setting the int property.
    gcd_entity = googledatastore.Entity()
    fake_property = flexmock()
    fake_property.should_receive("name").and_return("name")
    fake_property.should_receive("has_multiple").and_return(False)
    flexmock(mapper).should_receive("get_property_value").\
      and_return(pb_mapper.ValueType.INT, 1)
     
    property_list = [fake_property]
    mapper.set_properties(gcd_entity, property_list)
    self.assertEquals("name", gcd_entity.property[0].name)

  def test_convert_blind_put_request(self):
    ent = datastore_pb.EntityProto()
    key = ent.mutable_key()
    path = key.mutable_path()
    path.add_element()
    fake_request = flexmock()
    fake_request.should_receive("entity_list").and_return([ent])
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    gcd_blind_write = mapper.convert_blind_put_request(fake_request)
    self.assertEquals(True, gcd_blind_write.HasField("mutation"))

  def test_convert_blind_put_response(self):
    ent = datastore_pb.EntityProto()
    key = ent.mutable_key()
    path = key.mutable_path()
    new_element = path.add_element()
    new_element.set_name("name")
    fake_put_request = flexmock()
    put_response = datastore_pb.PutResponse()
    gcd_response = googledatastore.MutationResult()
    fake_put_request.should_receive("entity_list").and_return([ent])
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    mapper.convert_blind_put_response(fake_put_request, gcd_response, put_response)
    self.assertEquals("app_id", put_response.key_list()[0].app())

  def test_convert_get_request(self):
    get_request = datastore_pb.GetRequest()
    new_key = get_request.add_key()
    new_path = new_key.mutable_path()
    new_element = new_path.add_element()
    new_element.set_type("kind")
    new_element.set_id(1)
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    gcd_lookup = mapper.convert_get_request(get_request)
    for key in gcd_lookup.key:
      for element in key.path_element:
        self.assertEquals("kind", element.kind) 

  def test_convert_get_response(self):
    get_response = datastore_pb.GetResponse()
    lookup_response = googledatastore.LookupResponse()
    new_ent_result = lookup_response.found.add() 
    new_path = new_ent_result.entity.key.path_element.add()
    new_path.kind = "kind"
    new_path.id = 1
    new_prop = new_ent_result.entity.property.add()
    new_prop.name = "prop"
    new_value = new_prop.value.add() 
    new_value.string_value = "hello"
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    result = mapper.convert_get_response(lookup_response, get_response)
    self.assertEquals(1, result.entity_size())

  def test_convert_delete_request_to_blind_write(self):
    delete_request = datastore_pb.DeleteRequest()
    path = delete_request.add_key().path()
    element = path.add_element()
    element.set_id(1)
    element.set_type("kind")
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    result = mapper.convert_delete_request_to_blind_write(delete_request)
    self.assertEquals(True, result.HasField("mutation"))
 
  def test_fill_in_key(self):
    gcd_key = googledatastore.Key()
    path_element = entity_pb.Path_Element()
    path_element.set_type("kind")
    path_element.set_name("name")
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    mapper.fill_in_key(gcd_key, [path_element])
    for element in gcd_key.path_element:
      self.assertEquals("kind", element.kind)

  def test_convert_query_request(self):
    query_request = datastore_pb.Query()
    query_request.set_kind("kind")
    query_request.set_limit(10)
    query_request.set_offset(1)
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    gcd_query = mapper.convert_query_request(query_request)
    for kind in gcd_query.query.kind:
      self.assertEquals("kind", kind.name)
    self.assertEquals(10, gcd_query.query.limit)
    self.assertEquals(1, gcd_query.query.offset)

  def test_add_properties_to_entity_pb(self):
    gcd_entity = googledatastore.Entity()
    ent = datastore_pb.EntityProto()
    new_property = gcd_entity.property.add() 
    new_property.name = "prop"
    new_value = new_property.value.add()
    new_value.string_value = "value"
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    gcd_query = mapper.add_properties_to_entity_pb(ent, gcd_entity)
    self.assertEquals("prop", ent.property_list()[0].name())
 
  def test_convert_query_response(self):
    query_result = datastore_pb.QueryResult()
    gcd_result = googledatastore.RunQueryResponse() 
    gcd_result.batch.more_results = True
    gcd_result.batch.entity_result_type = googledatastore.EntityResult.KEY_ONLY
    mapper = pb_mapper.PbMapper(app_id="app_id", dataset="dataset")
    mapper.convert_query_response(gcd_result, query_result) 
    
    self.assertEquals(True, query_result.has_more_results())
    self.assertEquals(True, query_result.keys_only())

if __name__ == "__main__":
  unittest.main()    
