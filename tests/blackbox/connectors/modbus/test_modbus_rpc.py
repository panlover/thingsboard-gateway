from os import path
import logging

from pymodbus.constants import Endian
from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.payload import BinaryPayloadBuilder
import pymodbus.client as ModbusClient
from tb_rest_client.rest_client_ce import *
from simplejson import load, loads

from tests.base_test import BaseTest
from tests.test_utils.gateway_device_util import GatewayDeviceUtil

LOG = logging.getLogger("TEST")


class ModbusRpcTest(BaseTest):
    CONFIG_PATH = path.join(path.dirname(path.dirname(path.dirname(path.abspath(__file__)))),
                            "data" + path.sep + "modbus" + path.sep)

    client = None
    gateway = None
    device = None

    @classmethod
    def setUpClass(cls) -> None:
        super(ModbusRpcTest, cls).setUpClass()

        # ThingsBoard REST API URL
        url = GatewayDeviceUtil.DEFAULT_URL

        # Default Tenant Administrator credentials
        username = GatewayDeviceUtil.DEFAULT_USERNAME
        password = GatewayDeviceUtil.DEFAULT_PASSWORD

        with RestClientCE(url) as cls.client:
            cls.client.login(username, password)

            cls.gateway = cls.client.get_tenant_devices(10, 0, text_search='Gateway').data[0]
            assert cls.gateway is not None

            while not cls.is_gateway_connected():
                LOG.info('Gateway connecting to TB...')
                sleep(1)

            LOG.info('Gateway connected to TB')

            cls.device = cls.client.get_tenant_devices(10, 0, text_search='Temp Sensor').data[0]
            assert cls.device is not None

    @classmethod
    def tearDownClass(cls):
        super(ModbusRpcTest, cls).tearDownClass()

        client = ModbusClient.ModbusTcpClient('modbus-server', port=5021, framer=ModbusRtuFramer)
        client.connect()
        builder = BinaryPayloadBuilder(byteorder=Endian.Little,
                                       wordorder=Endian.Little)
        builder.add_string('abcd')
        builder.add_bits(
            [False, True, False, True, True, False, True, True, True, True, False, True, False, False, True, False])
        builder.add_8bit_int(-0x12)
        builder.add_8bit_uint(0x12)
        builder.add_16bit_int(-0x5678)
        builder.add_16bit_uint(0x1234)
        builder.add_32bit_int(-0x1234)
        builder.add_32bit_uint(0x12345678)
        builder.add_16bit_float(12.34375)
        builder.add_32bit_float(223546.34375)
        builder.add_32bit_float(-22.34)
        builder.add_64bit_int(-0xDEADBEEF)
        builder.add_64bit_uint(0x12345678DEADBEEF)
        builder.add_64bit_uint(0xDEADBEEFDEADBEED)
        builder.add_64bit_float(123.45)
        builder.add_64bit_float(-123.45)
        client.write_registers(0, builder.to_registers(), slave=1)
        client.close()

    @classmethod
    def is_gateway_connected(cls):
        """
        Check if the gateway is connected.

        Returns:
            bool: True if the gateway is connected, False otherwise.
        """

        try:
            return cls.client.get_attributes_by_scope(cls.gateway.id, 'SERVER_SCOPE', 'active')[0]['value']
        except IndexError:
            return False

    @classmethod
    def load_configuration(cls, config_file_path):
        with open(config_file_path, 'r', encoding="UTF-8") as config:
            config = load(config)
        return config

    def change_connector_configuration(self, config_file_path):
        """
        Change the configuration of the connector.

        Args:
            config_file_path (str): The path to the configuration file.

        Returns:
            tuple: A tuple containing the modified configuration and the response of the save_device_attributes method.
        """

        config = self.load_configuration(config_file_path)
        config['Modbus']['ts'] = int(time() * 1000)
        response = self.client.save_device_attributes(self.gateway.id, 'SHARED_SCOPE', config)
        sleep(3)
        return config, response


class ModbusRpcReadingTest(ModbusRpcTest):
    def test_input_registers_reading_rpc_little(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/input_registers_reading_rpc_little.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/input_registers_values_reading_little.json')

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            result = self.client.handle_two_way_device_rpc_request(self.device.id,
                                                                   {
                                                                       "method": rpc_tag,
                                                                       "params": rpc
                                                                   })
            self.assertEqual(result, expected_values[rpc_tag], f'Value is not equal for the next rpc: {rpc_tag}')

    def test_input_registers_reading_rpc_big(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/input_registers_reading_rpc_big.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/input_registers_values_reading_big.json')

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            result = self.client.handle_two_way_device_rpc_request(self.device.id,
                                                                   {
                                                                       "method": rpc_tag,
                                                                       "params": rpc
                                                                   })
            self.assertEqual(result, expected_values[rpc_tag], f'Value is not equal for the next rpc: {rpc_tag}')

    def test_holding_registers_reading_rpc_little(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/holding_registers_reading_rpc_little.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/holding_registers_values_reading_little.json')

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            result = self.client.handle_two_way_device_rpc_request(self.device.id,
                                                                   {
                                                                       "method": rpc_tag,
                                                                       "params": rpc
                                                                   })
            self.assertEqual(result, expected_values[rpc_tag], f'Value is not equal for the next rpc: {rpc_tag}')

    def test_holding_registers_reading_rpc_big(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/holding_registers_reading_rpc_big.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/holding_registers_values_reading_big.json')

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            result = self.client.handle_two_way_device_rpc_request(self.device.id,
                                                                   {
                                                                       "method": rpc_tag,
                                                                       "params": rpc
                                                                   })
            self.assertEqual(result, expected_values[rpc_tag], f'Value is not equal for the next rpc: {rpc_tag}')

    def test_coils_reading_rpc_little(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/coils_reading_rpc_little.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/discrete_and_coils_registers_values_reading_little.json')

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            result = self.client.handle_two_way_device_rpc_request(self.device.id,
                                                                   {
                                                                       "method": rpc_tag,
                                                                       "params": rpc
                                                                   })
            self.assertEqual(result, expected_values[rpc_tag], f'Value is not equal for the next rpc: {rpc_tag}')

    def test_coils_reading_rpc_big(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/coils_reading_rpc_big.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/discrete_and_coils_registers_values_reading_big.json')

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            result = self.client.handle_two_way_device_rpc_request(self.device.id,
                                                                   {
                                                                       "method": rpc_tag,
                                                                       "params": rpc
                                                                   })
            self.assertEqual(result, expected_values[rpc_tag], f'Value is not equal for the next rpc: {rpc_tag}')

    def test_discrete_inputs_reading_rpc_little(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/discrete_inputs_reading_rpc_little.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/discrete_and_coils_registers_values_reading_little.json')

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            result = self.client.handle_two_way_device_rpc_request(self.device.id,
                                                                   {
                                                                       "method": rpc_tag,
                                                                       "params": rpc
                                                                   })
            self.assertEqual(result, expected_values[rpc_tag], f'Value is not equal for the next rpc: {rpc_tag}')

    def test_discrete_inputs_reading_rpc_big(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/discrete_inputs_reading_rpc_big.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/discrete_and_coils_registers_values_reading_little.json')

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            result = self.client.handle_two_way_device_rpc_request(self.device.id,
                                                                   {
                                                                       "method": rpc_tag,
                                                                       "params": rpc
                                                                   })
            self.assertEqual(result, expected_values[rpc_tag], f'Value is not equal for the next rpc: {rpc_tag}')


class ModbusRpcWritingTest(ModbusRpcTest):
    def test_writing_input_registers_rpc_little(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/input_registers_writing_rpc_little.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/input_registers_values_writing_little.json')
        telemetry_keys = [key['tag'] for slave in config['Modbus']['configurationJson']['master']['slaves'] for key in
                          slave['timeseries']]

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            self.client.handle_two_way_device_rpc_request(self.device.id,
                                                          {
                                                              "method": rpc_tag,
                                                              "params": expected_values[rpc_tag]
                                                          })
        sleep(3)
        latest_ts = self.client.get_latest_timeseries(self.device.id, ','.join(telemetry_keys))
        for (_type, value) in expected_values.items():
            if _type == 'bits' and _type == '4bits':
                latest_ts[_type][0]['value'] = loads(latest_ts[_type][0]['value'])
            else:
                value = str(value)

            self.assertEqual(value, latest_ts[_type][0]['value'],
                             f'Value is not equal for the next telemetry key: {_type}')

    def test_writing_input_registers_rpc_big(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/input_registers_writing_rpc_big.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/input_registers_values_writing_big.json')
        telemetry_keys = [key['tag'] for slave in config['Modbus']['configurationJson']['master']['slaves'] for key in
                          slave['timeseries']]

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            self.client.handle_two_way_device_rpc_request(self.device.id,
                                                          {
                                                              "method": rpc_tag,
                                                              "params": expected_values[rpc_tag]
                                                          })
        sleep(3)
        latest_ts = self.client.get_latest_timeseries(self.device.id, ','.join(telemetry_keys))
        for (_type, value) in expected_values.items():
            if _type == 'bits':
                latest_ts[_type][0]['value'] = loads(latest_ts[_type][0]['value'])
            else:
                value = str(value)

            self.assertEqual(value, latest_ts[_type][0]['value'],
                             f'Value is not equal for the next telemetry key: {_type}')

    def test_writing_holding_registers_rpc_little(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/holding_registers_writing_rpc_little.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/holding_registers_values_writing_little.json')
        telemetry_keys = [key['tag'] for slave in config['Modbus']['configurationJson']['master']['slaves'] for key in
                          slave['timeseries']]

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            self.client.handle_two_way_device_rpc_request(self.device.id,
                                                          {
                                                              "method": rpc_tag,
                                                              "params": expected_values[rpc_tag]
                                                          })
        sleep(3)
        latest_ts = self.client.get_latest_timeseries(self.device.id, ','.join(telemetry_keys))
        for (_type, value) in expected_values.items():
            if _type == 'bits':
                latest_ts[_type][0]['value'] = loads(latest_ts[_type][0]['value'])
            else:
                value = str(value)

            self.assertEqual(value, latest_ts[_type][0]['value'],
                             f'Value is not equal for the next telemetry key: {_type}')

    def test_writing_holding_registers_rpc_big(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/holding_registers_writing_rpc_big.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/holding_registers_values_writing_big.json')
        telemetry_keys = [key['tag'] for slave in config['Modbus']['configurationJson']['master']['slaves'] for key in
                          slave['timeseries']]

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            self.client.handle_two_way_device_rpc_request(self.device.id,
                                                          {
                                                              "method": rpc_tag,
                                                              "params": expected_values[rpc_tag]
                                                          })
        sleep(3)
        latest_ts = self.client.get_latest_timeseries(self.device.id, ','.join(telemetry_keys))
        for (_type, value) in expected_values.items():
            if _type == 'bits':
                latest_ts[_type][0]['value'] = loads(latest_ts[_type][0]['value'])
            else:
                value = str(value)

            self.assertEqual(value, latest_ts[_type][0]['value'],
                             f'Value is not equal for the next telemetry key: {_type}')

    def test_writing_coils_rpc_little(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/coils_writing_rpc_little.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/discrete_and_coils_registers_values_writing_little.json')
        telemetry_keys = [key['tag'] for slave in config['Modbus']['configurationJson']['master']['slaves'] for key in
                          slave['timeseries']]

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            self.client.handle_two_way_device_rpc_request(self.device.id,
                                                          {
                                                              "method": rpc_tag,
                                                              "params": expected_values[rpc_tag]
                                                          })
        sleep(3)
        latest_ts = self.client.get_latest_timeseries(self.device.id, ','.join(telemetry_keys))
        for (_type, value) in expected_values.items():
            if _type == 'bits':
                latest_ts[_type][0]['value'] = loads(latest_ts[_type][0]['value'])
            else:
                value = str(value)

            self.assertEqual(value, latest_ts[_type][0]['value'],
                             f'Value is not equal for the next telemetry key: {_type}')

    def test_writing_coils_rpc_big(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/coils_writing_rpc_big.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/discrete_and_coils_registers_values_writing_big.json')
        telemetry_keys = [key['tag'] for slave in config['Modbus']['configurationJson']['master']['slaves'] for key in
                          slave['timeseries']]

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            self.client.handle_two_way_device_rpc_request(self.device.id,
                                                          {
                                                              "method": rpc_tag,
                                                              "params": expected_values[rpc_tag]
                                                          })
        sleep(3)
        latest_ts = self.client.get_latest_timeseries(self.device.id, ','.join(telemetry_keys))
        for (_type, value) in expected_values.items():
            if _type == 'bits':
                latest_ts[_type][0]['value'] = loads(latest_ts[_type][0]['value'])
            else:
                value = str(value)

            self.assertEqual(value, latest_ts[_type][0]['value'],
                             f'Value is not equal for the next telemetry key: {_type}')

    def test_writing_discrete_inputs_rpc_little(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/discrete_inputs_writing_rpc_little.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/discrete_and_coils_registers_values_writing_little.json')
        telemetry_keys = [key['tag'] for slave in config['Modbus']['configurationJson']['master']['slaves'] for key in
                          slave['timeseries']]

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            self.client.handle_two_way_device_rpc_request(self.device.id,
                                                          {
                                                              "method": rpc_tag,
                                                              "params": expected_values[rpc_tag]
                                                          })
        sleep(3)
        latest_ts = self.client.get_latest_timeseries(self.device.id, ','.join(telemetry_keys))
        for (_type, value) in expected_values.items():
            if _type == 'bits':
                latest_ts[_type][0]['value'] = loads(latest_ts[_type][0]['value'])
            else:
                value = str(value)

            self.assertEqual(value, latest_ts[_type][0]['value'],
                             f'Value is not equal for the next telemetry key: {_type}')

    def test_writing_discrete_inputs_rpc_big(self):
        (config, _) = self.change_connector_configuration(
            self.CONFIG_PATH + 'configs/rpc_configs/discrete_inputs_writing_rpc_big.json')
        sleep(3)
        expected_values = self.load_configuration(
            self.CONFIG_PATH + 'test_values/rpc/discrete_and_coils_registers_values_writing_big.json')
        telemetry_keys = [key['tag'] for slave in config['Modbus']['configurationJson']['master']['slaves'] for key in
                          slave['timeseries']]

        for rpc in config['Modbus']['configurationJson']['master']['slaves'][0]['rpc']:
            rpc_tag = rpc.pop('tag')
            self.client.handle_two_way_device_rpc_request(self.device.id,
                                                          {
                                                              "method": rpc_tag,
                                                              "params": expected_values[rpc_tag]
                                                          })
        sleep(3)
        latest_ts = self.client.get_latest_timeseries(self.device.id, ','.join(telemetry_keys))
        for (_type, value) in expected_values.items():
            if _type == 'bits':
                latest_ts[_type][0]['value'] = loads(latest_ts[_type][0]['value'])
            else:
                value = str(value)

            self.assertEqual(value, latest_ts[_type][0]['value'],
                             f'Value is not equal for the next telemetry key: {_type}')
