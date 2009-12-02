#
# Copyright 2009 Hans Lellelid
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#   http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Functional tests to test full stack (but not actual socket layer).
"""
import sys
import time
import unittest
import logging
import socket
import select
import threading
from Queue import Queue

# TEMP:
from SocketServer import BaseServer

from coilmq.frame import StompFrame
from coilmq.queue import QueueManager
from coilmq.topic import TopicManager

from coilmq.server.socketserver import StompServer, StompRequestHandler, ThreadedStompServer
from coilmq.util.buffer import StompFrameBuffer
from coilmq.store.memory import MemoryQueue
from coilmq.scheduler import FavorReliableSubscriberScheduler, RandomQueueScheduler

from coilmq.tests import mock

class BaseFunctionalTestCase(unittest.TestCase):
    """
    Base class for test cases provides the fixtures for setting up the multi-threaded
    unit test infrastructure.
    
    We use a combination of C{threading.Event} and C{Queue.Queue} objects to faciliate
    inter-thread communication and lock-stepping the assertions. 
    """
    def setUp(self):
        
        self.clients = []
        self.server_address = None # This gets set in the server thread.
        self.ready_event = threading.Event()
        
        qm = QueueManager(store=MemoryQueue(),
                          subscriber_scheduler=FavorReliableSubscriberScheduler(),
                          queue_scheduler=RandomQueueScheduler())
        tm = TopicManager()
        
        addr_bound = threading.Event()
        def start_server():
            self.server = TestStompServer(('127.0.0.1', 0),
                                          ready_event=self.ready_event,
                                          authenticator=None,
                                          queue_manager=self._queuemanager(),
                                          topic_manager=self._topicmanager())
            self.server_address = self.server.socket.getsockname()
            addr_bound.set()
            self.server.serve_forever()
            
        self.server_thread = threading.Thread(target=start_server, name='server')
        self.server_thread.start()
        self.ready_event.wait()
        addr_bound.wait()
    
    def _queuemanager(self):
        """
        Returns the configured L{QueueManager} instance to use.
        
        Can be overridden by subclasses that wish to change out any queue mgr parameters.
        
        @rtype: L{QueueManager}
        """
        return QueueManager(store=MemoryQueue(),
                            subscriber_scheduler=FavorReliableSubscriberScheduler(),
                            queue_scheduler=RandomQueueScheduler())
    
    def _topicmanager(self):
        """
        Returns the configured L{TopicManager} instance to use.
        
        Can be overridden by subclasses that wish to change out any topic mgr parameters.
        
        @rtype: L{TopicManager}
        """
        return TopicManager()
    
    def tearDown(self):
        for c in self.clients:
            print "Disconnecting %s" % c
            c.close()
        self.server.shutdown()
        self.server_thread.join()
        self.ready_event.clear()
        del self.server_thread
        
    def _new_client(self):
        """
        Get a new L{TestStompClient} connected to our test server. 
        
        The client will also be registered for close in the tearDown method.
        @rtype: L{TestStompClient}
        """
        client = TestStompClient(self.server_address)
        self.clients.append(client)
        client.connect()
        # print "Client created: %s" % (client)
        r = client.received_frames.get(timeout=1)
        assert r.cmd == 'CONNECTED'
        return client

class TestStompServer(ThreadedStompServer):
    """
    A stomp server for functional tests that uses C{threading.Event} objects
    to ensure that it stays in sync with the test suite.
    """
    
    allow_reuse_address = True
    
    def __init__(self, server_address,
                 bind_and_activate=True,
                 ready_event=None,
                 authenticator=None,
                 queue_manager=None,
                 topic_manager=None):
        self.ready_event = ready_event
        StompServer.__init__(self, server_address, StompRequestHandler,
                             bind_and_activate=bind_and_activate,
                             authenticator=authenticator,
                             queue_manager=queue_manager,
                             topic_manager=topic_manager)
        
    def server_activate(self):
        self.ready_event.set()
        StompServer.server_activate(self)

class TestStompClient(object):
    """
    A stomp client for use in testing.
    
    This client spawns a listener thread and pushes anything that comes in onto the 
    read_frames queue.
    
    @ivar received_frames: A queue of StompFrame instances that have been received.
    @type received_frames: C{Queue.Queue} containing any received L{coilmq.frame.StompFrame}
    """
    def __init__(self, addr, connect=True):
        """
        @param addr: The (host,port) tuple for connection.
        @type addr: C{tuple}
        
        @param connect: Whether to connect socket to specified addr.
        @type connect: C{bool}
        """
        self.log = logging.getLogger('%s.%s' % (self.__module__, self.__class__.__name__))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addr = addr
        self.received_frames = Queue()
        self.read_stopped = threading.Event()
        self.buffer = StompFrameBuffer()
        if connect:
            self._connect()
    
    def connect(self):
        self.send_frame(StompFrame('CONNECT'))
        
    def send(self, destination, message, set_content_length=True):
        headers = {'destination': destination}
        if set_content_length: headers['content-length'] = len(message)
        self.send_frame(StompFrame('SEND', headers=headers, body=message))
    
    def subscribe(self, destination):
        self.send_frame(StompFrame('SUBSCRIBE', headers={'destination': destination}))
        
    def send_frame(self, frame):
        """
        Sends a stomp frame.
        @param frame: The stomp frame to send.
        @type frame: L{coilmq.frame.StompFrame}
        """
        if not self.connected:
            raise RuntimeError("Not connected")
        self.sock.send(frame.pack())
    
    def _connect(self):
        self.sock.connect(self.addr)
        self.connected = True
        self.read_stopped.clear()
        t = threading.Thread(target=self._read_loop, name="client-receiver-%s" % hex(id(self)))
        t.start()
    
    def _read_loop(self):
        while self.connected:
            r, w, e = select.select([self.sock], [], [], 0.1)
            if r:
                data = self.sock.recv(1024)
                self.log.debug("Data received: %r" % data)
                self.buffer.append(data)
                for frame in self.buffer:
                    self.log.debug("Processing frame: %s" % frame)
                    self.received_frames.put(frame)
        self.read_stopped.set()
        # print "Read loop has been quit! for %s" % id(self)
    
    def disconnect(self):
        self.send_frame(StompFrame('DISCONNECT'))
        
    def close(self):
        if not self.connected:
            raise RuntimeError("Not connected")
        self.connected = False
        self.read_stopped.wait()
        self.sock.close()
