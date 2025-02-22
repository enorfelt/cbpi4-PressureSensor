
# -*- coding: utf-8 -*-
import os
from aiohttp import web
import logging
from unittest.mock import MagicMock, patch
import asyncio
import random
from cbpi.api import *
from cbpi.api.dataclasses import NotificationAction, NotificationType

##### Sensor requirements #####
import time
import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
###############################

logger = logging.getLogger(__name__)


@parameters([
    Property.Select(label="ADSchannel", options=[0,1,2,3], description="Enter channel-number of ADS1x15"),
    Property.Select("sensorType", options=["Voltage","Digits", "Pressure", "Pressure Compensated", "Liquid Level", "Liquid Level Compensated","Volume", "Volume Compensated"], description="Select which type of data to register for this sensor"),
    Property.Select("pressureType", options=["kPa","PSI"]),
    Property.Number("voltLow", configurable=True, default_value=0, description="Pressure Sensor minimum voltage, usually 0"),
    Property.Number("voltHigh", configurable=True, default_value=5, description="Pressure Sensor maximum voltage, usually 5"),
    Property.Number("pressureLow", configurable=True, default_value=0, description="Pressure value at minimum voltage, value in kPa"),
    Property.Number("pressureHigh", configurable=True, default_value=10, description="Pressure value at maximum voltage, value in kPa"),
    Property.Number("sensorHeight", configurable=True, default_value=0, description="Location of Sensor from the bottom of the kettle in cm"),
    Property.Number("kettleDiameter", configurable=True, default_value=0, description="Diameter of kettle in cm"),
    Property.Sensor("tempSensor",description="Select temperature sensor to be able to do volume compensation"),
])


class PressureSensor(CBPiSensor):
    
    def __init__(self, cbpi, id, props):
        super(PressureSensor, self).__init__(cbpi, id, props)
        self.value = 0
        self.lastValues = []
        # Variables to be used with calculations
        self.GRAVITY = 9.807
        self.PI = 3.1415
        # Conversion values
        self.kpa_psi = 0.145
        self.bar_psi = 14.5038
        self.inch_mm = 25.4
        self.gallons_cubicinch = 231
    
    def convert_pressure(self, value):
        if self.props.get("pressureType", "kPa") == "PSI":
            return value * self.kpa_psi
        else:
            return value
    
    def convert_bar(self, value):
        if self.props.get("pressureType", "kPa") == "PSI":
            return value / self.bar_psi
        else:
            return value / 100

    async def run(self):
        
        GRAVITY = float(self.GRAVITY)
        
        self.ADSchannel = int(self.props.get("ADSchannel", 0))
        pressureHigh = float(self.props.get("pressureHigh", 10)) #self.convert_pressure(int(self.props.get("pressureHigh", 10)))
        pressureLow = float(self.props.get("pressureLow", 0)) #self.convert_pressure(int(self.props.get("pressureLow", 0)))
        # logging.info('Pressure values - low: %s , high: %s' % ((pressureLow), (pressureHigh)))
        # We need the coefficients to calculate pressure for the next step
        # Using Y=MX+B where X is the volt output difference, M is kPa/volts or pressure difference / volt difference
        #  B is harder to explain, it's the offset of the voltage & pressure, ex:
        #    if volts were 1-5V and pressure was 0-6kPa
        #    since volts start with 1, there is an offset
        #    We calculate a value of 1.5kPa/V, therefore 1V = -1.5
        #    if the output of the sensor was 0-5V there would be no offset
        calcX = float(self.props.get("voltHigh", 5)) - float(self.props.get("voltLow", 0))
        # logging.info('calcX value: %s' % (calcX))
        calcM = (pressureHigh - pressureLow) / calcX
        # logging.info('calcM value: %s' % (calcM))
        calcB = 0
        if int(self.props.get("voltLow", 0)) > 0:
            calcB = (-1 * int(self.props.get("voltLow", 0))) * calcM
        #logging.info('calcB value: %s' % (calcB))

        zero_drift = 0.5  # %FS per degree Celsius
        sensitivity_drift = 0.5  # %FS per degree Celsius
        reference_temperature = 20  # degrees Celsius

        tempSensor = self.props.get("tempSensor", None)
        
        while self.running is True:
            
            # Create the I2C bus
            i2c = busio.I2C(board.SCL, board.SDA)
            
            # Create the ADS object using the I2C bus
            ads = ADS.ADS1115(i2c)
            
            # Create single-ended input on channel specified
            if self.ADSchannel == 0:
                chan = AnalogIn(ads, ADS.P0)
            elif self.ADSchannel == 1:
                chan = AnalogIn(ads, ADS.P1)
            elif self.ADSchannel == 2:
                chan = AnalogIn(ads, ADS.P2)
            elif self.ADSchannel == 3:
                chan = AnalogIn(ads, ADS.P3)
                
            temperature = reference_temperature # temp from sensor
            if tempSensor is not None:
                sensorValue = self.cbpi.sensor.get_sensor_value(tempSensor).get("value")
                temperature = round(float(sensorValue), 2)

            pressureValueRaw = (calcM * chan.voltage) + calcB    # "%.6f" % ((calcM * voltage) + calcB)

            temperature_coefficient = zero_drift + (sensitivity_drift / pressureValueRaw)
            pressureValueCompensated = pressureValueRaw / (1 + temperature_coefficient * (temperature - reference_temperature))

            #logging.info("pressureValueRaw: %s" % pressureValueRaw)    #debug or calibration
            #logging.info("pressureValueCompensated: %s" % pressureValueCompensated)    #debug or calibration
            
            # Time to calculate the other data values
            
            # Liquid Level is calculated by H = P / (SG * G). Assume the SG of water is 1.000
            #   this is true for water at 4C
            #   note: P needs to be in BAR and H value will need to be multiplied by 100 to get cm
            liquidLevel = (pressureValueRaw / GRAVITY) * 100 #/ self.inch_mm
            if liquidLevel > 0.49:
                liquidLevel += float(self.props.get("sensorHeight", 0))
            liquidLevelCompensated = (pressureValueCompensated / GRAVITY) * 100 #/ self.inch_mm
            if liquidLevelCompensated > 0.49:
                liquidLevelCompensated += float(self.props.get("sensorHeight", 0))
            

            # Volume is calculated by V = PI (r squared) * height
            kettleDiameter = float(self.props.get("kettleDiameter", 0))
            # logging.info("kettleDia: %s" % (kettleDiameter))    #debug or calibration

            kettleRadius = kettleDiameter / 2
            radiusSquared = kettleRadius * kettleRadius
            volumeCI = self.PI * radiusSquared * liquidLevel / 1000
            volume = volumeCI
            volumeCICompensated = self.PI * radiusSquared * liquidLevelCompensated / 1000
            volumeCompensated = volumeCICompensated

            # logging.info("volume: %s" % (volume))    #debug or calibration


            if self.props.get("sensorType", "Liquid Level") == "Voltage":
                self.value = chan.voltage
            elif self.props.get("sensorType", "Liquid Level") == "Digits":
                self.value = chan.value
            elif self.props.get("sensorType", "Liquid Level") == "Pressure":
                self.value = pressureValueRaw
            elif self.props.get("sensorType", "Liquid Level") == "Pressure Compensated":
                self.value = pressureValueCompensated
            elif self.props.get("sensorType", "Liquid Level") == "Liquid Level":
                self.value = liquidLevel
            elif self.props.get("sensorType", "Liquid Level") == "Liquid Level Compensated":
                self.value = liquidLevelCompensated
            elif self.props.get("sensorType", "Liquid Level") == "Volume":
                self.value = round(volume, 2)
            elif self.props.get("sensorType", "Liquid Level") == "Volume Compensated":
                self.value = round(volumeCompensated, 2)
                self.lastValues.insert(0, self.value)
                if (len(self.lastValues) > 5):
                    self.lastValues.pop()
                self.value = round(sum(self.lastValues) / len(self.lastValues), 2)
            else:
                self.value = chan.voltage

            # logging.info("push_update: %s" % (self.value))
            self.log_data(self.value)
            self.push_update(self.value)
            await asyncio.sleep(1)
    
    def get_state(self):
        return dict(value=self.value)


def setup(cbpi):
    cbpi.plugin.register("PressureSensor", PressureSensor)
    pass
