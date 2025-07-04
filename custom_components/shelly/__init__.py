"""
Support for Shelly smart home devices.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/shelly/
"""
# pylint: disable=broad-except, bare-except, invalid-name, import-error

from datetime import timedelta, datetime
import re
import shutil
import os
import logging
import asyncio
import voluptuous as vol
from awesomeversion import AwesomeVersion

try:
    from pyShelly import pyShelly as ImportedPyShelly
except Exception as e:
    ImportedPyShelly = None
    import_error = e

if os.getenv("SHELLY_DEBUGPY"):
    import debugpy
    debugpy.listen(5678)
    if os.getenv("SHELLY_DEBUGPY")=="wait":
        print("WAITING FOR DEBUGGER!")
        debugpy.wait_for_client()

from homeassistant.const import (
    CONF_DEVICES, CONF_DISCOVERY, CONF_ID, CONF_PASSWORD,
    CONF_SCAN_INTERVAL, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP,
    __version__ as HAVERSION )    
from homeassistant import config_entries
from homeassistant.helpers import (discovery, entity_registry as er)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.restore_state import RestoreStateData
from homeassistant.helpers.storage import Store
from homeassistant.helpers.json import JSONEncoder
try:
    from homeassistant.components.zeroconf import (
        async_get_instance as zeroconf_async_get_instance
    )
except:
    zeroconf_async_get_instance = None
try: #Backward compatible with HA
    from homeassistant.helpers.entity_registry import ATTR_RESTORED
except:
    ATTR_RESTORED = None
from homeassistant.helpers.entity import Entity
from homeassistant.util import slugify, dt as dt_util

try:
    from homeassistant.util import get_local_ip
except:
    get_local_ip = None

try: 
    from homeassistant.components.network import async_get_source_ip
except:
    async_get_source_ip = None

from .const import *
from .configuration_schema import CONFIG_SCHEMA, CONFIG_SCHEMA_ROOT
from .frontend import setup_frontend

_LOGGER = logging.getLogger(__name__)

__version__ = "1.0.10"
VERSION = __version__

async def async_setup(hass, config):
    """Set up this integration using yaml."""
    if DOMAIN not in config:
        return True
    data = dict(config.get(DOMAIN))
    hass.data["yaml_shelly"] = data
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data={}
        )
    )
    return True

async def async_setup_entry(hass, config_entry):
    """Setup Shelly component"""
    _LOGGER.info("Starting shelly, %s", __version__)

    if not DOMAIN in hass.data:
        hass.data[DOMAIN] = ShellyApp(hass)

    if config_entry.source == "import":
        if config_entry.options: #config.yaml
            data = config_entry.options.copy()
        else:
            if "yaml_shelly" in hass.data:
                data = hass.data["yaml_shelly"]
            else:
                data = {}
                await hass.config_entries.async_remove(config_entry.entry_id)
    else:
        data = config_entry.data.copy()
        data.update(config_entry.options)

    conf = CONFIG_SCHEMA_ROOT(data)

    if conf.get(CONF_WIFI_SENSOR) is not None:
        _LOGGER.warning("wifi_sensor is deprecated, use rssi in sensors instead.")
        if conf.get(CONF_WIFI_SENSOR) and SENSOR_RSSI not in conf[CONF_SENSORS]:
            conf[CONF_SENSORS].append(SENSOR_RSSI)

    if conf.get(CONF_UPTIME_SENSOR) is not None:
        _LOGGER.warning("uptime_sensor is deprecated, use uptime in sensors instead.")
        if conf.get(CONF_UPTIME_SENSOR) and SENSOR_UPTIME not in conf[CONF_SENSORS]:
            conf[CONF_SENSORS].append(SENSOR_UPTIME)

    hass.data[DOMAIN].instances[config_entry.entry_id] =  \
        ShellyInstance(hass, config_entry, conf)

    return True

async def async_unload_entry(hass, config_entry):
    """Unload a config entry."""
    instance = hass.data[DOMAIN].instances[config_entry.entry_id]
    await instance.stop()
    await instance.clean()
    return True

async def async_remove_config_entry_device(hass, config_entry, device_entry):    
    instance = hass.data[DOMAIN].instances[config_entry.entry_id]
    return True    

class ShellyApp():
    def __init__(self, hass):
        self.hass = hass
        self.instances = {}
        self.ha_version = AwesomeVersion(HAVERSION)
    def is_ver(self, ver):
        return self.ha_version >= AwesomeVersion(ver)
        
class ShellyInstance():
    """Config instance of Shelly"""
    def __init__(self, hass, config_entry, conf):
        self.hass = hass        
        self.cancel_update_listener = config_entry.add_update_listener(self.update_listener)
        self.config_entry = config_entry
        self.entry_id = self.config_entry.entry_id
        self.platforms = {}
        self.entities = []
        self.pys = None
        self.conf = conf
        self.version_added = False
        self.discover = self.conf.get(CONF_DISCOVERY)
        #self.device_sensors = []  #Removed, cause collisions
        self.block_sensors = []  #Keep track dynamic block sensors is added
        self.update_config_attributes()
        sensors = self.conf.get(CONF_SENSORS, {})
        if SENSOR_MQTT in sensors:
            self.conf[CONF_SENSORS].append(SENSOR_MQTT_CONNECTED)
            self.conf[CONF_SENSORS].remove(SENSOR_MQTT)
        if SENSOR_CLOUD in sensors:
            self.conf[CONF_SENSORS].append(SENSOR_CLOUD_STATUS)
            self.conf[CONF_SENSORS].remove(SENSOR_CLOUD)
        if SENSOR_DEFAULT in sensors:
            self.conf[CONF_SENSORS] = DEFAULT_SENSORS
        if SENSOR_ALL in sensors:
            self.conf[CONF_SENSORS] = [*ALL_SENSORS.keys()]        
        self._debug_msg = conf.get(CONF_DEBUG_ENABLE_INFO)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.stop)

        hass.loop.create_task(
            self.start_up()
        )
        self.shelly_config = {}
        hass.loop.create_task(
            setup_frontend(self)
        )

    async def update_listener(self, hass, config_entry):
        """Handle options update."""
        #print("###### CONFIG Updated ####################")
        #print(self.config_entry.options)
        self.hass.bus.fire('s4h/config_updated', {})
        
        options = CONFIG_SCHEMA_ROOT(self.config_entry.options.copy())
        self.conf.update(options)

        config_list = GLOBAL_CONFIG + DEBUG_CONFIG
        for key in config_list:
            if key in self.conf and \
                not key in options:
            #    self.conf[key] = self.config_entry.options[key]
            #elif key in self.conf:
                del self.conf[key]
        ##self.conf.update(self.config_entry.options)
        self.update_config_attributes()
        
        await self.update_config()

        entity_reg = \
            self.hass.helpers.entity_registry.async_get(self.hass)
        for entity in self.entities:
                if hasattr(entity, 'config_updated'):
                    entity.config_updated()

    def set_config_attribute(self, id, value):
        self.update_config_list(CONF_ATTRIBUTES, id, value)

    def set_config_sensor(self, id, value):
        self.update_config_list(CONF_SENSORS, id, value)

    def set_setting(self, id, param, value):
        options = self.config_entry.options.copy()  
        settings = options[CONF_SETTINGS] = \
            options[CONF_SETTINGS].copy() \
                if CONF_SETTINGS in options else {}
        settings[id] = settings[id].copy() if id in settings else {}
        if value!=None:
            settings[id][param] = value
        else:
            del settings[id][param]
            if not settings[id]:
                del settings[id]
                if not settings:
                    del options[CONF_SETTINGS]
        self.hass.config_entries.async_update_entry(
            self.config_entry, options=options)

    def set_config(self, id, value):
        options = self.config_entry.options.copy()
        if value!="":
            options[id]=value
        elif id in options:
            del options[id]
        self.hass.config_entries.async_update_entry(
            self.config_entry, options=options)

    def update_config_list(self, type, id, value):
        #options = self.config_entry.options.copy()
        options = self.conf.copy()
        list = options[type] = options.get(type, []).copy()
        if value:
            if not id in list:
                list.append(id)
        else:
            if id in list:
                list.remove(id)
        self.hass.config_entries.async_update_entry(
            self.config_entry, options=options)

    async def update_config(self):
        conf = self.conf
        pys = self.pys

        #if self.local_py_shelly != conf.get(CONF_LOCAL_PY_SHELLY):
        #    self.config_entry.async_unload(self.hass)??

        pys.igmp_fix_enabled = conf.get(CONF_IGMPFIX)
        pys.only_device_id = conf.get(CONF_ONLY_DEVICE_ID,"").upper()
        pys.update_status_interval = timedelta(seconds=conf.get(CONF_SCAN_INTERVAL, 0))
        self._debug_msg = conf.get(CONF_DEBUG_ENABLE_INFO)
        if conf.get(CONF_VERSION) and not self.version_added:
            self.version_added = True
            attr = {'version': VERSION, 'pyShellyVersion': pys.version(),
                    'extra' : {'ip-addr': pys.host_ip}}
            self.add_device("sensor", attr)
        if not conf.get(CONF_VERSION) and self.version_added:
            self.version_added = False
            entity_reg = \
                self.hass.helpers.entity_registry.async_get(self.hass)
            entity_id = "sensor." + slugify(conf.get(CONF_OBJECT_ID_PREFIX)) + "_version"
            entity_reg.async_remove(entity_id)

    def update_config_attributes(self):
        self.conf_attributes = set(self.conf.get(CONF_ATTRIBUTES))
        if ATTRIBUTE_ALL in self.conf_attributes:
            self.conf_attributes |= ALL_ATTRIBUTES
            self.conf_attributes.remove(ATTRIBUTE_ALL)
        if ATTRIBUTE_DEFAULT in self.conf_attributes:
            self.conf_attributes |= DEFAULT_ATTRIBUTES
            self.conf_attributes.remove(ATTRIBUTE_DEFAULT)
        if ATTRIBUTE_CONSUMPTION in self.conf_attributes:
            self.conf_attributes.add(ATTRIBUTE_CURRENT_CONSUMPTION)
            self.conf_attributes.add(ATTRIBUTE_TOTAL_CONSUMPTION)
            self.conf_attributes.add(ATTRIBUTE_TOTAL_RETURNED)
            self.conf_attributes.remove(ATTRIBUTE_CONSUMPTION)
        self.conf[CONF_ATTRIBUTES] = list(self.conf_attributes)
        if ATTRIBUTE_SWITCH in self.conf_attributes:
            self.conf_attributes.add(ATTRIBUTE_SWITCH + "_1")
            self.conf_attributes.add(ATTRIBUTE_SWITCH + "_2")

    async def start_up(self):
        self.shelly_config = await self.async_load_file("config") or {}
        last_ver = await self.async_get_config('version', '0.0.0')
        await self.async_set_config('version', VERSION)

        conf = self.conf
        self.local_py_shelly = conf.get(CONF_LOCAL_PY_SHELLY)
        if self.local_py_shelly:
            _LOGGER.info("Loading local pyShelly")
            from .pyShelly import pyShelly as LoadedPyShelly
        else:
            if not ImportedPyShelly:
                _LOGGER.error("Failed to import pyShelly: %s", import_error)
                return
            LoadedPyShelly = ImportedPyShelly

        additional_info = conf.get(CONF_ADDITIONAL_INFO)
        update_interval = conf.get(CONF_SCAN_INTERVAL)

        #self.pys = pys = pyShelly(self.hass.loop)
        self.pys = pys = LoadedPyShelly(self.hass.loop)
        _LOGGER.info("pyShelly, %s", pys.version())
        pys.cb_block_added.append(self._block_added)
        pys.cb_device_added.append(self._device_added)
        pys.cb_device_removed.append(self._device_removed)
        pys.cb_save_cache = self._save_cache
        pys.cb_load_cache = self._load_cache
        pys.username = conf.get(CONF_USERNAME)
        pys.password = conf.get(CONF_PASSWORD)
        pys.cloud_auth_key = conf.get(CONF_CLOUD_AUTH_KEY)
        pys.cloud_server = conf.get(CONF_CLOUD_SERVER)
        if zeroconf_async_get_instance:
            pys.zeroconf = await zeroconf_async_get_instance(self.hass)
        tmpl_name = conf.get(CONF_TMPL_NAME)
        if tmpl_name:
            pys.tmpl_name = tmpl_name
        #if additional_info:
        pys.update_status_interval = timedelta(seconds=update_interval)
        pys.only_device_id = conf.get(CONF_ONLY_DEVICE_ID)
        if pys.only_device_id:
            pys.only_device_id = pys.only_device_id.upper()
        pys.igmp_fix_enabled = conf.get(CONF_IGMPFIX)
        pys.mdns_enabled = conf.get(CONF_MDNS)
        pys.shelly_instance = self #Used for debuging only
        host_ip = conf.get(CONF_HOST_IP)
        if host_ip:
            pys.host_ip = host_ip
            if host_ip == 'ha':
                if get_local_ip:
                    pys.host_ip = get_local_ip() 
                elif async_get_source_ip:
                    pys.host_ip = async_get_source_ip() #from 2021-12 release
        pys.mqtt_port = conf.get(CONF_MQTT_PORT, 0)

        pys.mqtt_server_host = conf.get(CONF_MQTT_SERVER_HOST, '')
        pys.mqtt_server_port = conf.get(CONF_MQTT_SERVER_PORT, 1883)
        pys.mqtt_server_username = conf.get(CONF_MQTT_SERVER_USERNAME, '')
        pys.mqtt_server_password = conf.get(CONF_MQTT_SERVER_PASSWORD, '')

        pys.start()
        pys.discover()

        discover_by_ip = conf.get(CONF_DISCOVER_BY_IP)
        for ip_addr in discover_by_ip:
            pys.add_device_by_ip(ip_addr, 'IP-addr')

        if conf.get(CONF_VERSION):
            self.version_added = True
            attr = {'version': VERSION, 'pyShellyVersion': pys.version(),
                    'extra' : {'ip-addr': pys.host_ip}}
            self.add_device("sensor", attr)

        entity_reg = er.async_get(self.hass)
        entities_to_remove = []
        entities_to_fix_attr = []
        restore_expired = dt_util.as_utc(datetime.now()) - timedelta(hours=12)
        for entity in entity_reg.entities.values():
            if entity.platform == "shelly":
                entity_id = entity.entity_id
                entity_id = re.sub("_[0-9]+$", "", entity_id)
                unique_id = entity.unique_id.lower()
                if last_ver == '0.0.0':
                    #Remove entities that have change type
                    if entity_id.startswith("sensor.") and \
                    (entity_id.endswith("_switch") or \
                        entity_id.endswith("_power") or \
                        entity_id.endswith("_door_window") or \
                        entity_id.endswith("_flood") or \
                        entity_id.endswith("_mqtt_connected_attr") or \
                        entity_id.endswith("_over_temp_attr") or \
                        entity_id.endswith("_over_power_attr") \
                    ):
                        entities_to_remove.append(entity.entity_id)
                    elif entity_id.startswith("sensor.") and \
                        entity_id.endswith("_consumption") and \
                        not entity_id.endswith("total_consumption") and \
                        not entity_id.endswith("current_consumption"):
                        entities_to_remove.append(entity.entity_id)
                    elif entity_id.startswith("binary_sensor.") and \
                        entity_id.endswith("_cloud_status_attr"):
                        entities_to_remove.append(entity.entity_id)
                    elif entity_id.endswith("_attr"):
                        entities_to_fix_attr.append(entity.entity_id)
                if unique_id.endswith("_firmware_update"):
                    entities_to_remove.append(entity.entity_id)
                elif "_shdw" in unique_id or \
                   "_shwt" in unique_id or \
                   "_shht" in unique_id or \
                   "_shbtn_1" in unique_id:               
                    #todo check last_seen
                    data = await RestoreStateData.async_get_instance(self.hass)
                    if entity.entity_id in data.last_states:
                        data = data.last_states[entity.entity_id]
                        last_seen = dt_util.as_utc(data.last_seen)
                        if last_seen > restore_expired:
                            state = data.state
                            attr = dict(state.attributes)
                            if ATTR_RESTORED:
                                attr[ATTR_RESTORED] = True
                            self.hass.states.async_set(entity.entity_id, \
                                                        state.state, attr)

        for entity_id in entities_to_remove:
            entity_reg.async_remove(entity_id)

        for entity_id in entities_to_fix_attr:
            new_id = entity_id[0:-5]
            new_unique = new_id.split('.', 1)[1]
            entity_reg.async_update_entity(
                entity_id,
                new_entity_id=new_id,
                new_unique_id=new_unique
            )


    async def stop(self, _=None):
        """Stop Shelly."""
        _LOGGER.info("Shutting down Shelly")
        entity_reg = \
            self.hass.helpers.entity_registry.async_get(self.hass)
        # entities_to_remove = []
        # for entity in entity_reg.entities.values():
        #     if entity.platform == "shelly":
        #         entities_to_remove.append(entity.entity_id)
        # for entity_id in entities_to_remove:
        #     entity_reg.async_remove(entity_id)
        if self.cancel_update_listener:
            self.cancel_update_listener()
        if self.pys:
            self.pys.close()

    def format_value(self, settings, value, add_unit=False):
        if settings is not None \
            and (isinstance(value, int) or isinstance(value, float)):
            decimals = settings.get(CONF_DECIMALS, 0)
            div = settings.get(CONF_DIV)
            if div:
                value = value / div
            if decimals is not None:
                if decimals > 0:
                    value = round(value, decimals)
                elif decimals == 0:
                    value = round(value)
            if add_unit and CONF_UNIT in settings:
                value = str(value) + ' ' + settings[CONF_UNIT]
        return value

    def _update_settings(self, settings, update):
        #Combine settings on lower level
        for key, value in update.items():
            if key in settings:
                settings[key]=settings[key].copy()
                settings[key].update(value)
            else:
                settings[key]=value

    def get_settings(self, *ids):
        settings = DEFAULT_SETTINGS.copy()
        conf_settings = self.conf.get(CONF_SETTINGS)
        self._update_settings(settings, conf_settings)
        for device_id in ids:
            device_cfg = self._find_device_config(device_id)
            if device_cfg:
                conf_settings = device_cfg.get(CONF_SETTINGS)
                self._update_settings(settings, conf_settings)
        return settings

    def _get_specific_config_root(self, key, *ids):
        item = self._get_specific_config(key, None, *ids)
        if item is None:
            item = self.conf.get(key)
        return item

    def _find_device_config(self, device_id):
        device_conf_list = self.conf.get(CONF_DEVICES)
        for item in device_conf_list:
            if item[CONF_ID].upper() == device_id:
                return item
        return None

    def _get_device_config(self, device_id, id_2=None):
        """Get config for device."""
        item = self._find_device_config(device_id)
        if item is None and id_2 is not None:
            item = self._find_device_config(id_2)
        if item is None:
            return {}
        return item

    def _get_specific_config(self, key, default, *ids):
        for device_id in ids:
            item = self._find_device_config(device_id)
            if item is not None and key in item:
                return item[key]
        return default

    def _get_sensor_config(self, *ids):
        sensors = self._get_specific_config(CONF_SENSORS, None, *ids)
        if sensors is None:
            sensors = self.conf.get(CONF_SENSORS)
        if sensors is None:
            return {}
        if SENSOR_ALL in sensors:
            return [*ALL_SENSORS.keys()]
        return sensors

    def conf_attribute(self, key):
        return key in self.conf_attributes

    def add_device(self, platform, dev):
        self.hass.add_job(self._asyncadd_device, platform, dev)

    async def _asyncadd_device(self, platform, dev):
        if platform not in self.platforms:
            self.platforms[platform] = asyncio.Event()
            await self.hass.config_entries.async_forward_entry_setups(
                    self.config_entry, [platform])
            self.platforms[platform].set()

        await self.platforms[platform].wait()
        async_dispatcher_send(self.hass, "shelly_new_" + platform \
                                , dev, self)
        
    def _block_updated(self, block):
        self.hass.add_job(self._async_block_updated, block)

    async def _async_block_updated(self, block):
        hass_data = block.hass_data

        if hass_data['discover']:
            if hass_data['allow_upgrade_switch']:
                has_update = block.has_fw_update()
                #info_values.get('has_firmware_update', False)
                update_switch = getattr(block, 'firmware_switch', None)
                if has_update:
                    if update_switch is None:
                        attr = {'firmware': True, 'block':block, 'beta':False}
                        self.add_device("switch", attr)
                elif update_switch is not None:
                    update_switch.remove()
                    
            if hass_data['allow_upgrade_beta_switch']:
                has_beta_update = block.has_fw_update(True)
                update_beta_switch = getattr(block, 'beta_firmware_switch', None)
                if has_beta_update:
                    if update_beta_switch is None:
                        attr = {'firmware': True, 'block':block, 'beta':True}
                        self.add_device("switch", attr)
                elif update_beta_switch is not None:
                    update_beta_switch.remove()

            #block_key = _get_block_key(block)
            #entity_reg = \
            #    await self.hass.helpers.entity_registry.async_get(self.hass)
            info_values = block.info_values.copy()
            for key, _value in info_values.items():
                ukey = block.id + '-' + key
                if not ukey in self.block_sensors:
                    self.block_sensors.append(ukey)
                    for sensor in hass_data['sensor_cfg']:
                        if sensor in ALL_SENSORS and \
                            ALL_SENSORS[sensor].get('attr') == key:
                            attr = {'sensor_type':key,
                                    'itm': block,
                                    'ukey': ukey}
                            if key in SENSOR_TYPES_CFG and \
                                SENSOR_TYPES_CFG[key][4] == 'bool':
                                self.add_device("binary_sensor", attr)
                            else:
                                self.add_device("sensor", attr)

    def _block_added(self, block):
        self.hass.add_job(self._async_block_added, block)

    async def _async_block_added(self, block):
        block.cb_updated.append(self._block_updated)
        discover_block = self.discover \
                         or self._get_device_config(block.id) != {}

        block.hass_data = {
            'allow_upgrade_switch' :
                self._get_specific_config_root(CONF_UPGRADE_SWITCH, block.id),
            'allow_upgrade_beta_switch' :
                self._get_specific_config_root(CONF_UPGRADE_BETA_SWITCH, block.id),
            'sensor_cfg' : self._get_sensor_config(block.id),
            'discover': discover_block
        }

        #Config block
        if block.unavailable_after_sec is None:
            block.unavailable_after_sec \
                = self._get_specific_config_root(CONF_UNAVALABLE_AFTER_SEC,
                                            block.id)

        # dev_reg = await self.hass.helpers.device_registry.async_get()
        # dev_reg.async_get_or_create(
        #     config_entry_id=block.id,
        #     identifiers={(DOMAIN, block.id)},
        #     manufacturer="Allterco",
        #     name=block.friendly_name(),
        #     model=block.type_name(),
        #     sw_version=block.fw_version(),
        # )
        #if conf.get(CONF_ADDITIONAL_INFO):
            #block.update_status_information()
            # cfg_sensors = conf.get(CONF_SENSORS)
            # for sensor in cfg_sensors:
            #     sensor_type = ALL_SENSORS[sensor]
            #     if 'attr' in sensor_type:
            #         attr = {'sensor_type':sensor_type['attr'],
            #                 SHELLY_BLOCK_ID : block_key}
            #         discovery.load_platform(hass, 'sensor', DOMAIN, attr,
            #                                 config)

    def _device_added(self, dev, _code):
        self.hass.add_job(self._async_device_added, dev, _code)

    async def _async_device_added(self, dev, _code):
        device_config = self._get_device_config(dev.id, dev.block.id)
        if not self.discover and device_config == {}:
            return

        if dev.device_type == "ROLLER":
            self.add_device("cover", dev)
        elif dev.device_type == "RELAY":
            load_as_light = False
            if device_config.get(CONF_LIGHT_SWITCH):
                load_as_light = True
            elif dev.as_light():
                load_as_light = True
            if load_as_light:
                self.add_device("light", dev)
            else:
                self.add_device("switch", dev)
        elif dev.device_type == 'POWERMETER':
            sensor_cfg = self._get_sensor_config(dev.id, dev.block.id)
            if SENSOR_CURRENT_CONSUMPTION in sensor_cfg or \
                SENSOR_CONSUMPTION in sensor_cfg or \
                SENSOR_POWER in sensor_cfg: #POWER deprecated
                self.add_device("sensor", dev)
        elif dev.device_type == 'SWITCH':
            sensor_cfg = self._get_sensor_config(dev.id, dev.block.id)
            if SENSOR_SWITCH in sensor_cfg:
                self.add_device("binary_sensor", dev)
        elif dev.device_type == "SENSOR":
            self.add_device("sensor", dev)
        elif dev.device_type == "BINARY_SENSOR":
            self.add_device("binary_sensor", dev)
        elif dev.device_type in ["LIGHT", "DIMMER", "RGBLIGHT"]:
            self.add_device("light", dev)
        elif dev.device_type == 'TRV':
            self.add_device("climate", dev)
        else:
            _LOGGER.error("Unknown device type, %s", dev.device_type)

    async def clean(self):
        path = Store(self.hass, "1", "shelly/" + self.entry_id).path
        await self.hass.async_add_executor_job(shutil.rmtree, path)
        root_path = Store(self.hass, "1", "shelly").path
        if not os.listdir(root_path) :
            os.rmdir(root_path)

    def _device_removed(self, dev, _code):
        dev.shelly_device.remove()

    def _store(self, name):
        path = f"shelly/" + self.entry_id + "/" + name
        return Store(self.hass, "1", path, encoder=JSONEncoder)

    async def async_set_config(self, name, value):
        if self.shelly_config.get(name) != value:
            self.shelly_config[name] = value
            await self.async_save_file('config', self.shelly_config)

    async def async_get_config(self, name, default=None):
        return self.shelly_config.get(name, default)

    async def async_save_file(self, name, data):
        await self._store(name).async_save(data)

    async def async_load_file(self, name):
        return await self._store(name).async_load()

    def _save_cache(self, name, data):
        asyncio.run_coroutine_threadsafe(
            self._store(name).async_save(data), self.hass.loop
        )

    def _load_cache(self, name):
        data = asyncio.run_coroutine_threadsafe(
            self._store(name).async_load(), self.hass.loop
        ).result()
        return data
