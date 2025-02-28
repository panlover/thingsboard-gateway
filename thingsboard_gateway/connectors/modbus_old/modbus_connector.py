#     Copyright 2024. ThingsBoard
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
import logging
from copy import deepcopy
from threading import Thread, Lock
from time import sleep, monotonic
from time import monotonic as time
from queue import Queue
from random import choice
from string import ascii_lowercase
from typing import Union

from packaging import version

from thingsboard_gateway.gateway.entities.converted_data import ConvertedData
from thingsboard_gateway.gateway.statistics.statistics_service import StatisticsService
from thingsboard_gateway.tb_utility.tb_utility import TBUtility
from thingsboard_gateway.tb_utility.tb_logger import init_logger

# Try import Pymodbus library or install it and import
installation_required = False
required_version = '3.0.0'
force_install = False

try:
    from pymodbus import __version__ as pymodbus_version

    if version.parse(pymodbus_version) < version.parse(required_version):
        installation_required = True

    if version.parse(
            pymodbus_version) > version.parse(required_version):
        installation_required = True
        force_install = True

except ImportError:
    installation_required = True

if installation_required:
    print("Modbus library not found - installing...")
    TBUtility.install_package("pymodbus", required_version, force_install=force_install)
    TBUtility.install_package('pyserial')
    TBUtility.install_package('pyserial-asyncio')

try:
    from twisted.internet import reactor
except ImportError:
    TBUtility.install_package('twisted')
    from twisted.internet import reactor

from pymodbus.bit_write_message import WriteSingleCoilResponse, WriteMultipleCoilsResponse
from pymodbus.register_write_message import WriteMultipleRegistersResponse, WriteSingleRegisterResponse
from pymodbus.register_read_message import ReadRegistersResponseBase
from pymodbus.bit_read_message import ReadBitsResponseBase
from pymodbus.client import ModbusTcpClient, ModbusTlsClient, ModbusUdpClient, ModbusSerialClient
from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.framer.socket_framer import ModbusSocketFramer
from pymodbus.framer.ascii_framer import ModbusAsciiFramer
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.server import StartTcpServer, StartTlsServer, StartUdpServer, StartSerialServer, ServerStop
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.version import version
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.datastore import ModbusSparseDataBlock
from pymodbus.pdu import ExceptionResponse

from thingsboard_gateway.connectors.connector import Connector
from thingsboard_gateway.connectors.modbus_old.constants import *
from thingsboard_gateway.connectors.modbus_old.slave import Slave
from thingsboard_gateway.connectors.modbus_old.backward_compability_adapter import BackwardCompatibilityAdapter
from thingsboard_gateway.connectors.modbus_old.bytes_modbus_downlink_converter import BytesModbusDownlinkConverter

CONVERTED_DATA_SECTIONS = [ATTRIBUTES_PARAMETER, TELEMETRY_PARAMETER]
FRAMER_TYPE = {
    'rtu': ModbusRtuFramer,
    'socket': ModbusSocketFramer,
    'ascii': ModbusAsciiFramer
}
SLAVE_TYPE = {
    'tcp': StartTcpServer,
    'tls': StartTlsServer,
    'udp': StartUdpServer,
    'serial': StartSerialServer
}
FUNCTION_TYPE = {
    COILS_INITIALIZER: 'co',
    HOLDING_REGISTERS: 'hr',
    INPUT_REGISTERS: 'ir',
    DISCRETE_INPUTS: 'di'
}
FUNCTION_CODE_WRITE = {
    HOLDING_REGISTERS: (6, 16),
    COILS_INITIALIZER: (5, 15)
}
FUNCTION_CODE_SLAVE_INITIALIZATION = {
    HOLDING_REGISTERS: (6, 16),
    COILS_INITIALIZER: (5, 15),
    INPUT_REGISTERS: (6, 16),
    DISCRETE_INPUTS: (5, 15)
}
FUNCTION_CODE_READ = {
    HOLDING_REGISTERS: 3,
    COILS_INITIALIZER: 1,
    INPUT_REGISTERS: 4,
    DISCRETE_INPUTS: 2
}


class ModbusConnector(Connector, Thread):
    process_requests = Queue(-1)

    def __init__(self, gateway, config, connector_type):
        self.statistics = {STATISTIC_MESSAGE_RECEIVED_PARAMETER: 0,
                           STATISTIC_MESSAGE_SENT_PARAMETER: 0}
        super().__init__()
        self.__cached_connections = {}
        self.__gateway = gateway
        self._connector_type = connector_type
        self.__enable_remote_logging = config.get('enableRemoteLogging', False)
        self.__log = init_logger(self.__gateway, config.get('name', self.name),
                                 config.get('logLevel', 'INFO'),
                                 enable_remote_logging=self.__enable_remote_logging)
        self.__backward_compatibility_adapter = BackwardCompatibilityAdapter(config, gateway.get_config_path(),
                                                                             logger=self.__log)
        self.__config = self.__backward_compatibility_adapter.convert()
        self.__id = self.__config.get('id')
        self.name = self.__config.get("name", 'Modbus Connector ' + ''.join(choice(ascii_lowercase) for _ in range(5)))
        self.__replace_loggers()

        self.__connected = False
        self.__stopped = False
        self.__stopping = False
        self.daemon = True

        self.lock = Lock()

        self._convert_msg_queue = Queue()
        self._save_msg_queue = Queue()

        self.__msg_queue = Queue()
        self.__workers_thread_pool = []
        self.__max_msg_number_for_worker = config.get('maxMessageNumberPerWorker', 10)
        self.__max_number_of_workers = config.get('maxNumberOfWorkers', 100)

        self.__slaves = []
        self.__slave_thread = None

        if self.__config.get('slave') and self.__config.get('slave', {}).get('sendDataToThingsBoard', False):
            self.__slave_thread = Thread(target=self.__configure_and_run_slave, args=(self.__config['slave'],),
                                         daemon=True, name='Gateway modbus slave')
            self.__slave_thread.start()
        self.__load_slaves(self.__config.get('master', {'slaves': []}).get('slaves', []))

    def __replace_loggers(self):
        for logger_name in logging.root.manager.loggerDict.keys():
            if 'pymodbus' in logger_name:
                init_logger(self.__gateway,
                            logger_name,
                            'ERROR',
                            enable_remote_logging=self.__enable_remote_logging,
                            is_connector_logger=True,
                            connector_name=self.name)

    def is_connected(self):
        return self.__connected

    def is_stopped(self):
        return self.__stopped

    def open(self):
        stopping_started = time()
        while self.__stopping:
            sleep(.1)
            if (time() - stopping_started) > TIMEOUT:
                self.__log.error("Stopping timeout exceeded!")
                break
        self.__stopped = False
        self.__log.debug("Starting %s...", self.get_name())
        self.start()

    def get_type(self):
        return self._connector_type

    def run(self):
        self.__connected = True

        thread = Thread(target=self.__process_slaves, daemon=True, name="Modbus connector master processor thread")
        thread.start()

        self.__log.debug("%s connector with name %s started.", self.connector_type, self.get_name())
        while not self.__stopped:
            self.__thread_manager()

            sleep(.001)

    def __configure_and_run_slave(self, config):
        identity = None
        if config.get('identity'):
            identity = ModbusDeviceIdentification()
            identity.VendorName = config['identity'].get('vendorName', '')
            identity.ProductCode = config['identity'].get('productCode', '')
            identity.VendorUrl = config['identity'].get('vendorUrl', '')
            identity.ProductName = config['identity'].get('productName', '')
            identity.ModelName = config['identity'].get('ModelName', '')
            identity.MajorMinorRevision = version.short()

        blocks = {}
        if (config.get('values') is None) or (not len(config.get('values'))):
            self.__log.error("No values to read from device %s", config.get('deviceName', 'Modbus Slave'))
            return

        for (key, value) in config.get('values').items():
            values = {}
            converter = BytesModbusDownlinkConverter({}, self.__log)
            for section in ('attributes', 'timeseries', 'attributeUpdates', 'rpc'):
                for item in value.get(section, []):
                    function_code = FUNCTION_CODE_SLAVE_INITIALIZATION[key][0] if item['objectsCount'] <= 1 else \
                        FUNCTION_CODE_SLAVE_INITIALIZATION[key][1]
                    converted_value = converter.convert(
                        {**item,
                         'device': config.get('deviceName', 'Gateway'), 'functionCode': function_code,
                         'byteOrder': config['byteOrder'], 'wordOrder': config.get('wordOrder', 'LITTLE')},
                        {'data': {'params': item['value']}})
                    if converted_value is not None:
                        values[item['address'] + 1] = converted_value
                    else:
                        self.__log.error("Failed to convert value %s with type %s, skipping...", item['value'], item['type'])
                if len(values):
                    blocks[FUNCTION_TYPE[key]] = ModbusSparseDataBlock(values)

        if not len(blocks):
            self.__log.info("%s - will be initialized without values", config.get('deviceName', 'Modbus Slave'))

        self.__add_slave_to_devices()

        context = ModbusServerContext(slaves=ModbusSlaveContext(**blocks), single=True)
        SLAVE_TYPE[config['type']](context=context, identity=identity,
                                   address=(config.get('host'), config.get('port')) if (
                                           config['type'] == 'tcp' or 'udp') else None,
                                   port=config.get('port') if config['type'] == 'serial' else None,
                                   framer=FRAMER_TYPE[config['method']], **config.get('security', {}))

    def __add_slave_to_devices(self):
        config = self.__config['slave']

        values = config.pop('values')
        device = config

        for (register, reg_values) in values.items():
            for (section_name, section_values) in reg_values.items():
                if not device.get(section_name):
                    device[section_name] = []

                for item in section_values:
                    device[section_name].append({**item, 'functionCode': FUNCTION_CODE_READ[
                        register] if section_name not in ('attributeUpdates', 'rpc') else item['functionCode']})

        self.__config['master']['slaves'].append(device)
        self.__load_slave(device)

    def __load_slaves(self, slaves):
        for slave in slaves:
            self.__load_slave(slave)

    def __load_slave(self, slave):
        slave_config = {**slave, 'connector': self, 'gateway': self.__gateway, 'logger': self.__log,
                        'callback': ModbusConnector.callback}
        self.__slaves.append(Slave(**slave_config))

    @classmethod
    def callback(cls, slave: Slave, request_type: RequestType, data=None):
        cls.process_requests.put((slave, request_type, data))

    @property
    def connector_type(self):
        return self._connector_type

    def __thread_manager(self):
        if len(self.__workers_thread_pool) == 0:
            worker = ModbusConnector.ConverterWorker("Main", self._convert_msg_queue, self._save_data, self.__log)
            self.__workers_thread_pool.append(worker)
            worker.start()

        number_of_needed_threads = round(self._convert_msg_queue.qsize() / self.__max_msg_number_for_worker, 0)
        threads_count = len(self.__workers_thread_pool)
        if number_of_needed_threads > threads_count < self.__max_number_of_workers:
            thread = ModbusConnector.ConverterWorker(
                "Converter worker " + ''.join(choice(ascii_lowercase) for _ in range(5)), self._convert_msg_queue,
                self._save_data, self.__log)
            self.__workers_thread_pool.append(thread)
            thread.start()
        elif number_of_needed_threads < threads_count and threads_count > 1:
            worker: ModbusConnector.ConverterWorker = self.__workers_thread_pool[-1]
            if not worker.in_progress:
                worker.close()
                self.__workers_thread_pool.remove(worker)

    def __convert_data(self, params):
        device, current_device_config, config, device_responses = params
        converted_data: Union[ConvertedData, None] = None

        try:
            converted_data = device.config[UPLINK_PREFIX + CONVERTER_PARAMETER].convert(
                config=config,
                data=device_responses)
        except Exception as e:
            self.__log.error("Failed to convert data from device %s with config %s", device.device_name, current_device_config, exc_info=e)

        if converted_data is not None and converted_data.attributes_datapoints_count + converted_data.telemetry_datapoints_count > 0:
            return converted_data

    def _save_data(self, data):
        StatisticsService.count_connector_message(self.name, stat_parameter_name='storageMsgPushed')

        self.__gateway.send_to_storage(self.get_name(), self.get_id(), data)
        self.statistics[STATISTIC_MESSAGE_SENT_PARAMETER] += 1

    def close(self):
        self.__stopped = True
        self.__stopping = True
        self.__log.debug("Stopping %s...", self.get_name())
        self.__stop_connections_to_masters()

        # Stop all slaves
        for slave in self.__slaves:
            slave.close()

        if self.__slave_thread is not None:
            try:
                ServerStop()
            except AttributeError:
                self.__slave_thread.join()

        # Stop all workers
        for worker in self.__workers_thread_pool:
            worker.close()

        # self.__slave_thread.join()
        self.__log.info('%s has been stopped.', self.get_name())
        self.__log.stop()
        self.__stopping = False

    def get_name(self):
        return self.name

    def get_id(self):
        return self.__id

    def __process_slaves(self):
        while not self.__stopped:
            if not self.__stopped and not ModbusConnector.process_requests.empty():
                (device, request_type, data) = ModbusConnector.process_requests.get()
                if request_type == RequestType.POLL:
                    self.__poll_device(device)
                elif request_type == RequestType.SEND_DATA:
                    self.__send_data_from_device_by_strategy(device, data)
            sleep(.001)

    def __send_data_from_device_by_strategy(self, device, data):
        self.__gateway.send_to_storage(self.get_name(), self.get_id(), data)
        self.statistics[STATISTIC_MESSAGE_SENT_PARAMETER] += 1


    def __poll_device(self, device):
        device_connected = device.last_connect_time != 0 and monotonic() - device.last_connect_time < 10
        device_disconnected = False

        self.__log.debug("Checking %s", device)
        if device.config.get(TYPE_PARAMETER).lower() == 'serial':
            self.lock.acquire()

        device_responses = {'timeseries': {}, 'attributes': {}}
        current_device_config = {}
        try:
            for config_section in device_responses:
                if device.config.get(config_section) is not None and len(device.config.get(config_section)):
                    current_device_config = device.config
                    connected_to_current_master = self.__connect_to_current_master(device)
                    if connected_to_current_master:
                        is_socket_open = device.config['master'].is_socket_open()
                        if not device_connected and is_socket_open:
                            device_connected = self.__gateway.add_device(device.device_name, {CONNECTOR_PARAMETER: self},
                                                                         device_type=device.config.get(DEVICE_TYPE_PARAMETER))

                            device.last_connect_time = monotonic() if device_connected else 0
                        elif not is_socket_open:
                            device.last_connect_time = 0
                            device_disconnected = True
                    else:
                        if not device_disconnected:
                            device.last_connect_time = 0
                            device_disconnected = True
                            self.__gateway.del_device(device.device_name)
                        continue

                    if (not device.config['master'].is_socket_open()
                            or not len(current_device_config[config_section])):
                        if not device.config['master'].is_socket_open():
                            error = 'Socket is closed, connection is lost, for device %s with config %s' % (
                                device.device_name, current_device_config)
                        else:
                            error = 'Config is invalid or empty for device %s, config %s' % (
                                device.device_name, current_device_config)
                        self.__log.error(error)
                        self.__log.debug("Device %s is not connected, data will not be processed",
                                         device.device_name)
                        continue

                    # Reading data from device
                    for interested_data in range(len(current_device_config[config_section])):
                        current_data = deepcopy(current_device_config[config_section][interested_data])
                        current_data[DEVICE_NAME_PARAMETER] = device.device_name
                        try:
                            input_data = self.__function_to_device(device, current_data)
                        except Exception as e:
                            input_data = e

                        # due to issue #1056
                        if isinstance(input_data, Exception):
                            device.config.pop('master', None)
                            self.__gateway.del_device(device.device_name)
                            self.__connect_to_current_master(device)
                            break

                        device_responses[config_section][current_data[TAG_PARAMETER]] = {
                            "data_sent": current_data,
                            "input_data": input_data
                        }

                    self.__log.debug("Checking %s for device %s", config_section, device)
                    self.__log.debug('Device response: ', device_responses)

            if device_responses.get('timeseries') or device_responses.get('attributes'):
                self._convert_msg_queue.put((self.__convert_data, (device, current_device_config, {
                    **current_device_config,
                    BYTE_ORDER_PARAMETER: current_device_config.get(BYTE_ORDER_PARAMETER, device.byte_order),
                    WORD_ORDER_PARAMETER: current_device_config.get(WORD_ORDER_PARAMETER, device.word_order)
                }, device_responses)))

        except ConnectionException:
            self.__gateway.del_device(device.device_name)
            sleep(5)
            self.__log.error("Connection lost! Reconnecting...")
        except Exception as e:
            self.__gateway.del_device(device.device_name)
            self.__log.exception("Error while processing device %s", device.device_name, exc_info=e)
        finally:
            # Release mutex if "serial" type only
            if device.config.get(TYPE_PARAMETER) == 'serial':
                self.lock.release()

    def __connect_to_current_master(self, device: Slave=None):
        connect_attempt_count = 5
        connect_attempt_time_ms = 100
        wait_after_failed_attempts_ms = 300000

        force_update_master = device.config['connection_attempt'] > 0
        if device.config.get('master') is None or force_update_master:
            device.config['master'], device.config['available_functions'] = self.__get_or_create_connection(device.config, force_update_master)

        if connect_attempt_count < 1:
            connect_attempt_count = 1

        connect_attempt_time_ms = device.config.get('connectAttemptTimeMs', connect_attempt_time_ms)

        if connect_attempt_time_ms < 500:
            connect_attempt_time_ms = 500

        wait_after_failed_attempts_ms = device.config.get('waitAfterFailedAttemptsMs', wait_after_failed_attempts_ms)

        if wait_after_failed_attempts_ms < 1000:
            wait_after_failed_attempts_ms = 1000

        current_time = time() * 1000

        if not device.config['master'].is_socket_open():
            if (device.config['connection_attempt'] >= connect_attempt_count
                    and current_time - device.config['last_connection_attempt_time'] >= wait_after_failed_attempts_ms):
                device.config['connection_attempt'] = 0

            while not device.config['master'].is_socket_open() \
                    and device.config['connection_attempt'] < connect_attempt_count \
                    and current_time - device.config.get('last_connection_attempt_time', 0) >= connect_attempt_time_ms:
                if self.__stopped:
                    return False
                device.config['connection_attempt'] = device.config['connection_attempt'] + 1
                device.config['last_connection_attempt_time'] = current_time
                self.__log.debug("Modbus trying connect to %s", device)
                device.config['master'].connect()

                if device.config['connection_attempt'] == connect_attempt_count:
                    self.__log.warn("Maximum attempt count (%i) for device \"%s\" - encountered.",
                                    connect_attempt_count,
                                    device)
                    return False

        if device.config['connection_attempt'] >= 0 and device.config['master'].is_socket_open():
            device.config['connection_attempt'] = 0
            device.config['last_connection_attempt_time'] = current_time
            return True
        else:
            return False

    @staticmethod
    def __configure_master(config):
        current_config = config
        current_config["rtu"] = FRAMER_TYPE[current_config['method']]

        if current_config.get(TYPE_PARAMETER) == 'tcp' and current_config.get('tls'):
            master = ModbusTlsClient(current_config["host"],
                                     current_config["port"],
                                     current_config["rtu"],
                                     timeout=current_config["timeout"],
                                     retry_on_empty=current_config["retry_on_empty"],
                                     retry_on_invalid=current_config["retry_on_invalid"],
                                     retries=current_config["retries"],
                                     **current_config['tls'])
        elif current_config.get(TYPE_PARAMETER) == 'tcp':
            master = ModbusTcpClient(current_config["host"],
                                     current_config["port"],
                                     current_config["rtu"],
                                     timeout=current_config["timeout"],
                                     retry_on_empty=current_config["retry_on_empty"],
                                     retry_on_invalid=current_config["retry_on_invalid"],
                                     retries=current_config["retries"])
        elif current_config.get(TYPE_PARAMETER) == 'udp':
            master = ModbusUdpClient(current_config["host"],
                                     current_config["port"],
                                     current_config["rtu"],
                                     timeout=current_config["timeout"],
                                     retry_on_empty=current_config["retry_on_empty"],
                                     retry_on_invalid=current_config["retry_on_invalid"],
                                     retries=current_config["retries"])
        elif current_config.get(TYPE_PARAMETER) == 'serial':
            master = ModbusSerialClient(method=current_config["method"],
                                        port=current_config["port"],
                                        timeout=current_config["timeout"],
                                        retry_on_empty=current_config["retry_on_empty"],
                                        retry_on_invalid=current_config["retry_on_invalid"],
                                        retries=current_config["retries"],
                                        baudrate=current_config["baudrate"],
                                        stopbits=current_config["stopbits"],
                                        bytesize=current_config["bytesize"],
                                        parity=current_config["parity"],
                                        strict=current_config["strict"])
        else:
            raise Exception("Invalid Modbus transport type.")

        available_functions = {
            1: master.read_coils,
            2: master.read_discrete_inputs,
            3: master.read_holding_registers,
            4: master.read_input_registers,
            5: master.write_coil,
            6: master.write_register,
            15: master.write_coils,
            16: master.write_registers,
        }
        return master, available_functions

    def __get_or_create_connection(self, config, force=False):
        keys_for_cache = ('host', 'port', 'method', 'type')
        if config.get(TYPE_PARAMETER) == 'serial':
            keys_for_cache = ('port', 'method')

        configuration_values_for_cache = tuple([config[key] for key in keys_for_cache])

        if self.__cached_connections.get(configuration_values_for_cache) is None or force:
            self.__cached_connections[configuration_values_for_cache] = self.__configure_master(config)
        return self.__cached_connections[configuration_values_for_cache]

    def __stop_connections_to_masters(self):
        for slave in self.__slaves:
            if (slave.config.get('master') is not None
                    and slave.config.get('master').is_socket_open()):
                slave.config['master'].close()

    def __function_to_device(self, device, config):
        function_code = config.get('functionCode')
        result = None
        if function_code == 1:
            result = device.config['available_functions'][function_code](address=config[ADDRESS_PARAMETER],
                                                                         count=config.get(OBJECTS_COUNT_PARAMETER,
                                                                                          config.get("registersCount",
                                                                                                     config.get(
                                                                                                         "registerCount",
                                                                                                         1))),
                                                                         slave=device.config['unitId'])
        elif function_code in (2, 3, 4):
            result = device.config['available_functions'][function_code](address=config[ADDRESS_PARAMETER],
                                                                         count=config.get(OBJECTS_COUNT_PARAMETER,
                                                                                          config.get("registersCount",
                                                                                                     config.get(
                                                                                                         "registerCount",
                                                                                                         1))),
                                                                         slave=device.config['unitId'])
        elif function_code == 5:
            result = device.config['available_functions'][function_code](address=config[ADDRESS_PARAMETER],
                                                                         value=config[PAYLOAD_PARAMETER],
                                                                         slave=device.config['unitId'])
        elif function_code == 6:
            result = device.config['available_functions'][function_code](address=config[ADDRESS_PARAMETER],
                                                                         value=config[PAYLOAD_PARAMETER],
                                                                         slave=device.config['unitId'])
        elif function_code in (15, 16):
            result = device.config['available_functions'][function_code](address=config[ADDRESS_PARAMETER],
                                                                         values=config[PAYLOAD_PARAMETER],
                                                                         slave=device.config['unitId'])
        else:
            self.__log.error("Unknown Modbus function with code: %s", function_code)

        self.__log.debug("With result %s", str(result))

        if "Exception" in str(result) or "Error" in str(result):
            self.__log.error("Reading failed for device %s function code %s address %s unit id %s",
                             device.device_name, function_code, config[ADDRESS_PARAMETER], device.config['unitId'])
            self.__log.error("Reading failed with exception:", exc_info=result)
            self.__log.info("Trying to reconnect to device %s", device.device_name)
            if device.config.get('master') is not None and device.config['master'].is_socket_open():
                device.config['master'].close()
            device.config['master'], device.config['available_functions'] = self.__get_or_create_connection(device.config, force=True)
            if self.__connect_to_current_master(device):
                self.__log.info("Reconnected to device %s", device.device_name)
                result = self.__function_to_device(device, config)
                if "Exception" in str(result) or "Error" in str(result):
                    self.__log.error("Reading failed for device %s function code %s address %s unit id %s",
                                     device.device_name, function_code, config[ADDRESS_PARAMETER], device.config['unitId'])
                    self.__log.error("Reading failed with exception:", exc_info=result)
                    if device.config.get('master') is not None and device.config['master'].is_socket_open():
                        device.config['master'].close()
                    device.config['master'] = None
                    self.__log.info("Will try to connect to device %s later", device.device_name)


        self.__log.debug("Sending request to device with unit id: %s, on address: %s, function code: %r using "
                         "connection: %r",
                         device.config['unitId'], config[ADDRESS_PARAMETER], function_code, device.config['master'])

        StatisticsService.count_connector_message(self.name, stat_parameter_name='connectorMsgsReceived')
        StatisticsService.count_connector_bytes(self.name, result, stat_parameter_name='connectorBytesReceived')

        return result

    def on_attributes_update(self, content):
        try:
            device = ModbusConnector.__get_device_by_name(content[DEVICE_SECTION_PARAMETER], self.__slaves)
            if device is None:
                self.__log.error("Device %s not found for connector %s", content[DEVICE_SECTION_PARAMETER], self.get_name())
                return
            for attribute_updates_command_config in device.config['attributeUpdates']:
                for attribute_updated in content[DATA_PARAMETER]:
                    if attribute_updates_command_config[TAG_PARAMETER] == attribute_updated:
                        to_process = {
                            DEVICE_SECTION_PARAMETER: content[DEVICE_SECTION_PARAMETER],
                            DATA_PARAMETER: {
                                RPC_METHOD_PARAMETER: attribute_updated,
                                RPC_PARAMS_PARAMETER: content[DATA_PARAMETER][attribute_updated]
                            }
                        }
                        attribute_updates_command_config['byteOrder'] = device.byte_order or 'LITTLE'
                        attribute_updates_command_config['wordOrder'] = device.word_order or 'LITTLE'
                        self.__process_request(to_process, attribute_updates_command_config,
                                               request_type='attributeUpdates')
        except Exception as e:
            self.__log.exception(e)

    def server_side_rpc_handler(self, server_rpc_request):
        try:
            if server_rpc_request.get('data') is None:
                server_rpc_request['data'] = {'params': server_rpc_request['params'],
                                              'method': server_rpc_request['method']}

            rpc_method = server_rpc_request['data']['method']

            # check if RPC type is connector RPC (can be only 'set')
            try:
                (connector_type, rpc_method_name) = rpc_method.split('_')
                if connector_type == self._connector_type:
                    rpc_method = rpc_method_name
                    server_rpc_request['device'] = server_rpc_request['params'].split(' ')[0].split('=')[-1]
            except (IndexError, ValueError, AttributeError):
                pass

            if server_rpc_request.get(DEVICE_SECTION_PARAMETER) is not None:
                self.__log.debug("Modbus connector received rpc request for %s with server_rpc_request: %s",
                                 server_rpc_request[DEVICE_SECTION_PARAMETER],
                                 server_rpc_request)
                device = ModbusConnector.__get_device_by_name(server_rpc_request[DEVICE_SECTION_PARAMETER],
                                                              self.__slaves)

                if device is None:
                    self.__log.error("Device %s not found for connector %s", server_rpc_request[DEVICE_SECTION_PARAMETER],
                                     self.get_name())
                    self.__gateway.send_rpc_reply(server_rpc_request[DEVICE_SECTION_PARAMETER],
                                                  server_rpc_request[DATA_PARAMETER][RPC_ID_PARAMETER],
                                                  {rpc_method: "DEVICE CONNECTOR FOR DEVICE NOT FOUND!"})
                    return

                # check if RPC method is reserved get/set
                if rpc_method == 'get' or rpc_method == 'set':
                    params = {}
                    for param in server_rpc_request['data']['params'].split(';'):
                        try:
                            (key, value) = param.split('=')
                        except ValueError:
                            continue

                        if key and value:
                            params[key] = value if key not in ('functionCode', 'objectsCount', 'address') else int(
                                value)

                    self.__process_request(server_rpc_request, params)
                elif isinstance(device.config[RPC_SECTION], dict):
                    rpc_command_config = device.config[RPC_SECTION].get(rpc_method)

                    if rpc_command_config is not None:
                        self.__process_request(server_rpc_request, rpc_command_config)

                elif isinstance(device.config[RPC_SECTION], list):
                    for rpc_command_config in device.config[RPC_SECTION]:
                        if rpc_command_config[TAG_PARAMETER] == rpc_method:
                            self.__process_request(server_rpc_request, rpc_command_config)
                            break
                else:
                    self.__log.error("Received rpc request, but method %s not found in config for %s.",
                                     rpc_method,
                                     self.get_name())
                    self.__gateway.send_rpc_reply(server_rpc_request[DEVICE_SECTION_PARAMETER],
                                                  server_rpc_request[DATA_PARAMETER][RPC_ID_PARAMETER],
                                                  {rpc_method: "METHOD NOT FOUND!"})
            else:
                self.__log.debug("Received RPC to connector: %r", server_rpc_request)
                results = []
                for device in self.__slaves:
                    server_rpc_request[DEVICE_SECTION_PARAMETER] = device.device_name
                    results.append(self.__process_request(server_rpc_request, server_rpc_request['params'], return_result=True))

                return results

        except Exception as e:
            self.__log.exception("Error during RPC handling: %s", exc_info=e)

    def __process_request(self, content, rpc_command_config, request_type='RPC', return_result=False):
        self.__log.debug('Processing %s request', request_type)
        if rpc_command_config is not None:
            device = ModbusConnector.__get_device_by_name(content[DEVICE_SECTION_PARAMETER], self.__slaves)
            if device is None:
                self.__log.error("Device %s not found for connector %s", content[DEVICE_SECTION_PARAMETER], self.get_name())
                return
            rpc_command_config[UNIT_ID_PARAMETER] = device.config['unitId']
            rpc_command_config[BYTE_ORDER_PARAMETER] = device.config.get("byteOrder", "LITTLE")
            rpc_command_config[WORD_ORDER_PARAMETER] = device.config.get("wordOrder", "LITTLE")
            self.__connect_to_current_master(device)

            if rpc_command_config.get(FUNCTION_CODE_PARAMETER) in (5, 6):
                converted_data = device.config[DOWNLINK_PREFIX + CONVERTER_PARAMETER].convert(rpc_command_config,
                                                                                              content)
                try:
                    rpc_command_config[PAYLOAD_PARAMETER] = converted_data[0]
                except IndexError and TypeError:
                    rpc_command_config[PAYLOAD_PARAMETER] = converted_data
            elif rpc_command_config.get(FUNCTION_CODE_PARAMETER) in (15, 16):
                converted_data = device.config[DOWNLINK_PREFIX + CONVERTER_PARAMETER].convert(rpc_command_config,
                                                                                              content)
                rpc_command_config[PAYLOAD_PARAMETER] = converted_data

            try:
                response = self.__function_to_device(device, rpc_command_config)
            except Exception as e:
                self.__log.exception(e)
                response = e

            if isinstance(response, (ReadRegistersResponseBase, ReadBitsResponseBase)):
                to_converter = {
                    RPC_SECTION: {
                        content[DATA_PARAMETER][RPC_METHOD_PARAMETER]: {
                            "data_sent": rpc_command_config,
                            "input_data": response
                        }
                    }
                }
                response = device.config[
                    UPLINK_PREFIX + CONVERTER_PARAMETER].convert(
                    config={**device.config,
                            BYTE_ORDER_PARAMETER: device.byte_order,
                            WORD_ORDER_PARAMETER: device.word_order
                            },
                    data=to_converter)
                self.__log.debug("Received %s method: %s, result: %r", request_type,
                                 content[DATA_PARAMETER][RPC_METHOD_PARAMETER],
                                 response)
            elif isinstance(response, (WriteMultipleRegistersResponse,
                                       WriteMultipleCoilsResponse,
                                       WriteSingleCoilResponse,
                                       WriteSingleRegisterResponse)):
                self.__log.debug("Write %r", str(response))
                response = {"success": True}

            if content.get(RPC_ID_PARAMETER) or (content.get(DATA_PARAMETER) is not None
                    and content[DATA_PARAMETER].get(RPC_ID_PARAMETER)) is not None:
                if isinstance(response, Exception) or isinstance(response, ExceptionResponse):
                    if not return_result:
                        self.__gateway.send_rpc_reply(device=content[DEVICE_SECTION_PARAMETER],
                                                      req_id=content[DATA_PARAMETER].get(RPC_ID_PARAMETER),
                                                      content={
                                                          content[DATA_PARAMETER][RPC_METHOD_PARAMETER]: str(response)
                                                      },
                                                      success_sent=False)
                    else:
                        return {
                            'device': content[DEVICE_SECTION_PARAMETER],
                            'req_id': content[DATA_PARAMETER].get(RPC_ID_PARAMETER),
                            'content': {
                                content[DATA_PARAMETER][RPC_METHOD_PARAMETER]: str(response)
                            },
                            'success_sent': False
                        }
                else:
                    if not return_result:
                        self.__gateway.send_rpc_reply(device=content[DEVICE_SECTION_PARAMETER],
                                                      req_id=content[DATA_PARAMETER].get(RPC_ID_PARAMETER),
                                                      content=response)
                    else:
                        return {
                            'device': content[DEVICE_SECTION_PARAMETER],
                            'req_id': content[DATA_PARAMETER].get(RPC_ID_PARAMETER),
                            'content': response
                        }

            self.__log.debug("%r", response)

    @staticmethod
    def __get_device_by_name(device_name, devices):
        filtered_device = tuple(filter(lambda slave: slave.device_name == device_name, devices))
        return filtered_device[0] if len(filtered_device) > 0 else None

    def get_config(self):
        return self.__config

    def update_converter_config(self, converter_name, config):
        self.__log.debug('Received remote converter configuration update for %s with configuration %s', converter_name,
                         config)
        for slave in self.__slaves:
            try:
                if slave.config[UPLINK_PREFIX + CONVERTER_PARAMETER].__class__.__name__ == converter_name:
                    slave.config.update(config)
                    self.__log.info('Updated converter configuration for: %s with configuration %s',
                                    converter_name, config)

                    for slave_config in self.__config['master']['slaves']:
                        if slave_config['deviceName'] == slave.device_name:
                            slave_config.update(config)

                    self.__gateway.update_connector_config_file(self.name, self.__config)
            except KeyError:
                continue

    class ConverterWorker(Thread):
        def __init__(self, name, incoming_queue, send_result, logger):
            super().__init__()
            self._log = logger
            self.__stopped = False
            self.name = name
            self.daemon = True
            self.__msg_queue = incoming_queue
            self.in_progress = False
            self.__send_result = send_result

        def run(self):
            while not self.__stopped:
                if not self.__msg_queue.empty():
                    self.in_progress = True
                    convert_function, params = self.__msg_queue.get(True, 10)
                    converted_data: ConvertedData = convert_function(params)
                    if converted_data:
                        self._log.info("Converted data for device %r attributes: %r, telemetry: %r",
                                       converted_data.device_name, converted_data.attributes_datapoints_count,
                                       converted_data.telemetry_datapoints_count)
                        self.__send_result(converted_data)
                        self.in_progress = False
                else:
                    sleep(.001)

        def close(self):
            self.__stopped = True