#!/usr/bin/env python
 
# import normal packages
import platform 
import logging
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file
 
# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusGoeChargerService:
  def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP JSON service'):
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    
    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
    self._paths = paths
    
    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))
    
    paths_wo_unit = [
      '/Status',  # value 'car' 1: charging station ready, no vehicle 2: vehicle loads 3: Waiting for vehicle 4: Charge finished, vehicle still connected
      '/Mode'
    ]
    
    #get data from go-eCharger
    data = self._getGoeChargerData()

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)
    
    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    self._dbusservice.add_path('/ProductId', 0xFFFF) # 
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', productname)    
    self._dbusservice.add_path('/FirmwareVersion', int(data['fwv'].replace('.', '')))
    self._dbusservice.add_path('/HardwareVersion', 2)
    self._dbusservice.add_path('/Serial', data['sse'])
    self._dbusservice.add_path('/ChargingTime', 0)  # not availabel on go-eCharger
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/UpdateIndex', 0)
    
    # add paths without units
    for path in paths_wo_unit:
      self._dbusservice.add_path(path, None)
    
    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # last update
    self._lastUpdate = 0

    # add _update function 'timer'
    gobject.timeout_add(250, self._update) # pause 250ms before the next request
    
    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)
 
  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config
 
 
  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']
    
    if not value: 
        value = 0
    
    return int(value)
  
  
  def _getGoeChargerStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']
    
    if accessType == 'OnPremise': 
        URL = "http://%s/status" % (config['ONPREMISE']['Host'])
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
    
    return URL
    
 
  def _getGoeChargerData(self):
    URL = self._getGoeChargerStatusUrl()
    request_data = requests.get(url = URL)
    
    # check for response
    if not request_data:
        raise ConnectionError("No response from go-eCharger - %s" % (URL))
    
    json_data = request_data.json()     
    
    # check for Json
    if not json_data:
        raise ValueError("Converting response to JSON failed")
    
    
    return json_data
 
 
  def _signOfLife(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %s" % (self._lastUpdate))
    logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    logging.info("--- End: sign of life ---")
    return True
 
  def _update(self):   
    try:
       #get data from go-eCharger
       data = self._getGoeChargerData()
       
       config = self._getConfig()
       
       #send data to DBus
       self._dbusservice['/Ac/L1/Power'] = int(data['nrg'][7] * 0.1 * 1000)
       self._dbusservice['/Ac/L2/Power'] = int(data['nrg'][8] * 0.1 * 1000)
       self._dbusservice['/Ac/L3/Power'] = int(data['nrg'][9] * 0.1 * 1000)
       self._dbusservice['/Ac/Power'] = int(data['nrg'][11] * 0.01 * 1000)
       self._dbusservice['/Ac/Voltage'] = int(data['nrg'][0])
       self._dbusservice['/Current'] = int(data['amp'])
       self._dbusservice['/SetCurrent'] = int(data['amp'])
       self._dbusservice['/MaxCurrent'] = 16  # data['vehicle_current_a']
       self._dbusservice['/Ac/Energy/Forward'] = int(float(data['eto']) / 10.0)
       #self._dbusservice['/ChargingTime'] = data['session_s']

       self._dbusservice['/Mode'] = 0  # Manual, no control
       self._dbusservice['/MCU/Temperature'] = int(data['tmp'])

       # value 'car' 1: charging station ready, no vehicle 2: vehicle loads 3: Waiting for vehicle 4: Charge finished, vehicle still connected
       status = 0
       if int(data['car']) == 1:
         status = 0
       elif int(data['car']) == 2:
         status = 2
       elif int(data['car']) == 3:
         status = 6
       elif int(data['car']) == 4:
         status = 3
       self._dbusservice['/Status'] = status

       #logging
       logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
       logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
       logging.debug("---")
       
       # increment UpdateIndex - to show that new data is available
       index = self._dbusservice['/UpdateIndex'] + 1  # increment index
       if index > 255:   # maximum value of the index
         index = 0       # overflow from 255 to 0
       self._dbusservice['/UpdateIndex'] = index

       #update lastupdate vars
       self._lastUpdate = time.time()              
    except Exception as e:
       logging.critical('Error at %s', '_update', exc_info=e)
       
    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True
 
  def _handlechangedvalue(self, path, value):
    logging.debug("someone else updated %s to %s" % (path, value))
    return True # accept the change
 


def main():
  #configure logging
  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=logging.DEBUG,
                            handlers=[
                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])
 
  try:
      logging.info("Start")
  
      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)
     
      #formatting 
      _kwh = lambda p, v: (str(round(v, 2)) + 'KWh')
      _a = lambda p, v: (str(round(v, 1)) + 'A')
      _w = lambda p, v: (str(round(v, 1)) + 'W')
      _v = lambda p, v: (str(round(v, 1)) + 'V')
      _degC = lambda p, v: (str(v) + '°C')
     
      #start our main-service
      pvac_output = DbusGoeChargerService(
        servicename='com.victronenergy.evcharger',
        paths={
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
          
          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          '/Current': {'initial': 0, 'textformat': _a},
          '/SetCurrent': {'initial': 0, 'textformat': _a},
          '/MaxCurrent': {'initial': 0, 'textformat': _a},
          '/MCU/Temperature': {'initial': 0, 'textformat': _degC}
        }
        )
     
      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()            
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
