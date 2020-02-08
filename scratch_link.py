#!/usr/bin/env python3

"""Scratch link on bluepy"""

import asyncio
import pathlib
import ssl
import websockets
import json
import base64

# for BLESession
from bluepy.btle import Scanner, UUID, Peripheral, DefaultDelegate
from bluepy.btle import BTLEDisconnectError
import threading
import time
import queue

# for logging
import logging
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)
logger.propagate = False

class Session():
    """Base class for BTSession and BLESession"""
    def __init__(self, websocket, loop):
        self.websocket = websocket
        self.loop = loop
        self.lock = threading.RLock()
        self.notification_queue = queue.Queue()

    async def recv_request(self):
        """
        Handle a request from Scratch through websocket.
        Return True when the sessino should end.
        """
        logger.debug("start recv_request")
        req = await self.websocket.recv()
        logger.debug(f"request: {req}")
        jsonreq = json.loads(req)
        if jsonreq['jsonrpc'] != '2.0':
            logger.error("error: jsonrpc versino is not 2.0")
            return
        jsonres = self.handle_request(jsonreq['method'], jsonreq['params'])
        if 'id' in jsonreq:
            jsonres['id'] = jsonreq['id']
        response = json.dumps(jsonres)
        logger.debug(f"response: {response}")
        await self.websocket.send(response)
        if self.end_request():
            return True
        return False

    def handle_request(self, method, params):
        """Default request handler"""
        logger.debug(f"default handle_request: {method}, {params}")

    def end_request(self):
        """
        Default callback at request end. This callback is required to
        allow other websocket usage out of the request handler.
        Return true when the session should end.
        """
        logger.debug("default end_request")
        return False

    def notify(self):
        """
        Notify BT/BLE device events to scratch.
        """
        logger.debug("start to notify")
        self.flush_notification_queue()

    def flush_notification_queue(self):
        merged_notifications = []
        while not self.notification_queue.empty():
            method, params = self.notification_queue.get()
            merged_notifications.append(self._create_notification_message(method, params))
        self._send_notification('\n'.join(merged_notifications))

    def _create_notification_message(self, method, params):
        jsonn = { 'jsonrpc': "2.0", 'method': method }
        jsonn['params'] = params
        notification = json.dumps(jsonn)
        logger.debug(f"notification: {notification}")
        return notification

    def _send_notification(self, merged_notifications):
        future = asyncio.run_coroutine_threadsafe(
            self.websocket.send(merged_notifications), self.loop)
        result = future.result()

    async def handle(self):
        logger.debug("start session hanlder")
        await self.recv_request()
        await asyncio.sleep(0.1)
        while True:
            if await self.recv_request():
                break
            logger.debug("in handle loop")

class BTSession(Session):
    """Manage a session for Bluetooh device"""
    def __init__(self, websocket):
        super().__init__(websocekt)

    def handle(self):
        logger.error("BT session handler is not implemented")

class BLESession(Session):
    """
    Manage a session for Bluetooh Low Energy device such as micro:bit
    """

    INITIAL = 1
    DISCOVERY = 2
    CONNECTED = 3
    DONE = 4

    ADTYPE_COMP_16B = 0x3
    ADTYPE_COMP_128B = 0x7

    class BLEThread(threading.Thread):
        """
        Separated thread to control notifications to Scratch.
        It handles device discovery notification in DISCOVERY status
        and notifications from BLE devices in CONNECTED status.
        """
        def __init__(self, session):
            threading.Thread.__init__(self)
            self.session = session

        def run(self):
            while True:
                logger.debug("loop in BLE thread")
                if self.session.status == self.session.DISCOVERY:
                    logger.debug("send out found devices")
                    devices = self.session.found_devices
                    for d in devices:
                        params = { 'rssi': d.rssi }
                        params['peripheralId'] = devices.index(d)
                        params['name'] = d.getValueText(0x9)
                        self.session.notification_queue.put(('didDiscoverPeripheral', params))
                        self.session.notify()
                    time.sleep(1)
                elif self.session.status == self.session.CONNECTED:
                    logger.debug("in connected status:")
                    delegate = self.session.delegate
                    if delegate and len(delegate.handles) > 0:
                        if not delegate.restart_notification_event.is_set():
                            delegate.restart_notification_event.wait()
                        try:
                            with self.session.lock:
                                self.session.perip.waitForNotifications(1.0)
                        except Exception as e:
                            logger.error(e)
                            self.session.close()
                            break
                    else:
                        time.sleep(0.0)
                    # To avoid repeated lock by this single thread,
                    # yield CPU to other lock waiting threads.
                    time.sleep(0)
                else:
                    # Nothing to do:
                    time.sleep(1)

    class BLEDelegate(DefaultDelegate):
        """
        A bluepy handler to receive notifictions from BLE devices.
        """
        def __init__(self, session):
            DefaultDelegate.__init__(self)
            self.session = session
            self.handles = {}
            self.restart_notification_event = threading.Event()
            self.restart_notification_event.set()

        def add_handle(self, serviceId, charId, handle):
            logger.debug(f"add handle for notification: {handle}")
            params = { 'serviceId': UUID(serviceId).getCommonName(),
                       'characteristicId': charId,
                       'encoding': 'base64' }
            self.handles[handle] = params

        def handleNotification(self, handle, data):
            logger.debug(f"BLE notification: {handle} {data}")
            params = self.handles[handle].copy()
            params['message'] = base64.standard_b64encode(data).decode('ascii')
            self.session.notification_queue.put(('characteristicDidChange', params))
            if not self.restart_notification_event.is_set():
                return
            self.session.notify()

    def __init__(self, websocket, loop):
        super().__init__(websocket, loop)
        self.status = self.INITIAL
        self.found_devices = []
        self.device = None
        self.perip = None
        self.delegate = None

    def close(self):
        self.status = self.DONE
        if self.perip:
            logger.info(f"disconnect to BLE peripheral: {self.perip}")
            self.perip.disconnect()

    def __del__(self):
        self.close()

    def matches(self, dev, filters):
        """
        Check if the found BLE device mathces the filters Scracth specifies.
        """
        logger.debug(f"in matches {dev} {filters}")
        for f in filters:
            if 'services' in f:
                for s in f['services']:
                    logger.debug(f"sevice to check: {s}")
                    given_uuid = s
                    logger.debug(f"given: {given_uuid}")
                    service_class_uuid = dev.getValueText(self.ADTYPE_COMP_128B)
                    logger.debug(f"adtype 128b: {service_class_uuid}")
                    if not service_class_uuid:
                        service_class_uuid = dev.getValueText(self.ADTYPE_COMP_16B)
                        logger.debug(f"adtype 16b: {service_class_uuid}")
                        if not service_class_uuid:
                            continue
                    dev_uuid = UUID(service_class_uuid)
                    logger.debug(f"dev: {dev_uuid}")
                    logger.debug(given_uuid == dev_uuid)
                    if given_uuid == dev_uuid:
                        logger.debug("match...")
                        return True
            if 'name' in f or 'manufactureData' in f:
                logger.error("name/manufactureData filters not implemented")
                # TODO: implement other filters defined:
                # ref: https://github.com/LLK/scratch-link/blob/develop/Documentation/BluetoothLE.md
        return False

    def handle_request(self, method, params):
        """Handle requests from Scratch"""
        if self.delegate:
            # Do not allow notification during request handling to avoid
            # websocket server errors
            self.delegate.restart_notification_event.clear()

        logger.debug("handle request to BLE device")
        logger.debug(method)
        if len(params) > 0:
            logger.debug(params)

        res = { "jsonrpc": "2.0" }

        if self.status == self.INITIAL and method == 'discover':
            scanner = Scanner()
            devices = scanner.scan(5.0)
            for dev in devices:
                logger.debug(f"found device: {dev.getScanData()}")
                if self.matches(dev, params['filters']):
                    self.found_devices.append(dev)
            if len(self.found_devices) == 0:
                err_msg = f"BLE service not found for {params['filters']}"
                res["error"] = { "message": err_msg }
                self.status = self.DONE
            else:
                res["result"] = None
                self.status = self.DISCOVERY
                self.ble_thread = self.BLEThread(self)
                self.ble_thread.start()

        elif self.status == self.DISCOVERY and method == 'connect':
            logger.debug("connecting to the BLE device")
            self.device = self.found_devices[params['peripheralId']]
            try:
                with self.lock:
                    self.perip = Peripheral(self.device.addr,
                                            self.device.addrType)
                logger.info(f"connect to BLE peripheral: {self.perip}")
            except BTLEDisconnectError as e:
                logger.error(f"failed to connect to BLE device: {e}")
                self.status = self.DONE

            if self.perip:
                res["result"] = None
                self.status = self.CONNECTED
                self.delegate = self.BLEDelegate(self)
                self.perip.withDelegate(self.delegate)
            else:
                err_msg = f"BLE connect failed :{self.device}"
                res["error"] = { "message": err_msg }
                self.status = self.DONE

        elif self.status == self.CONNECTED and method == 'read':
            logger.debug("handle read request")
            service_id = params['serviceId']
            chara_id = params['characteristicId']
            with self.lock:
                charas = self.perip.getCharacteristics(uuid=chara_id)
            c = charas[0]
            if c.uuid != UUID(chara_id):
                logger.error("Failed to get characteristic {chara_id}")
                self.status = self.DONE
            else:
                with self.lock:
                    b = c.read()
                message = base64.standard_b64encode(b).decode('ascii')
                res['result'] = { 'message': message, 'encode': 'base64' }

            try:
                startNotifications = params['startNotifications']
            except KeyError:
                logger.debug('No startNotifications param in request, defaulting to False')
                startNotifications = False

            if startNotifications == True:
                self.startNotifications(service_id=service_id, chara_id=chara_id)

        elif self.status == self.CONNECTED and method == 'startNotifications':
            logger.debug("handle startNotifications request")
            service_id = params['serviceId']
            chara_id = params['characteristicId']
            with self.lock:
                charas = self.perip.getCharacteristics(uuid=chara_id)
            self.startNotifications(service_id=service_id, chara_id=chara_id)

        elif self.status == self.CONNECTED and method == 'stopNotifications':
            logger.debug("handle stopNotifications request")
            service_id = params['serviceId']
            chara_id = params['characteristicId']
            with self.lock:
                charas = self.perip.getCharacteristics(uuid=chara_id)
            self.stopNotifications(service_id=service_id, chara_id=chara_id)

        elif self.status == self.CONNECTED and method == 'write':
            logger.debug("handle write request")
            service_id = params['serviceId']
            chara_id = params['characteristicId']
            with self.lock:
                charas = self.perip.getCharacteristics(uuid=chara_id)
            c = charas[0]
            if c.uuid != UUID(chara_id):
                logger.error("Failed to get characteristic {chara_id}")
                self.status = self.DONE
            else:
                if params['encoding'] != 'base64':
                    logger.error("encoding other than base 64 is not "
                                 "yet supported: ", params['encoding'])
                msg_bstr = params['message'].encode('ascii')
                data = base64.standard_b64decode(msg_bstr)
                with self.lock:
                    c.write(data)
                res['result'] = len(data)

        logger.debug(res)
        return res

    def setNotifications(self, service_id, chara_id, value):
        with self.lock:
            service = self.perip.getServiceByUUID(UUID(service_id))
            chas = service.getCharacteristics(forUUID=chara_id)
        handle = chas[0].getHandle()
        # prepare notification handler
        self.delegate.add_handle(service_id, chara_id, handle)
        # request notification to the BLE device
        with self.lock:
            self.perip.writeCharacteristic(chas[0].getHandle() + 1,
                                           b"\x01\x00", True)

    def startNotifications(self, service_id, chara_id):
        logger.debug(f"start notification for {chara_id}")
        value = b"\x01\x00"
        self.setNotifications(service_id=service_id, chara_id=chara_id, value=value)

    def stopNotifications(self, service_id, chara_id):
        logger.debug(f"stop notification for {chara_id}")
        value = b"\x00\x00"
        self.setNotifications(service_id=service_id, chara_id=chara_id, value=value)

    def end_request(self):
        logger.debug("end_request of BLESession")
        if self.delegate:
            self.delegate.restart_notification_event.set()
        return self.status == self.DONE

# kick start WSS server
ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
localhost_pem = pathlib.Path(__file__).with_name("scratch-device-manager.pem")
ssl_context.load_cert_chain(localhost_pem)
sessionTypes = { '/scratch/ble': BLESession, '/scratch/bt': BTSession }

async def ws_handler(websocket, path):
    try:
        logger.info(f"Start session for web socket path: {path}");
        loop = asyncio.get_event_loop()
        session = sessionTypes[path](websocket, loop)
        await session.handle()
    except Exception as e:
        logger.error(f"Failure in session for web socket path: {path}");
        logger.error(e);

start_server = websockets.serve(
    ws_handler, "device-manager.scratch.mit.edu", 20110, ssl=ssl_context
)

while True:
    try:
        asyncio.get_event_loop().run_until_complete(start_server)
        asyncio.get_event_loop().run_forever()
    except Exception as e:
        logger.info("restart server...")

