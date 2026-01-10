# MIT License
#
# Copyright (c) 2021 Arne Pagel
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Contributing author(s):
# - Arne Pagel

import logging
import jukebox.plugs as plugs
import jukebox.cfghandler
from components.battery_monitor import BatteryMonitorBase

logger = logging.getLogger('jb.battmon')

batt_mon = None


class battmon_sysfs(BatteryMonitorBase.BattmonBase):
    """Battery Monitor reading the current battery voltage from a configurable sysfs path.

    See [Battery Monitor documentation](../../builders/components/power/batterymonitor.md)
    """

    def __init__(self, cfg):
        super().__init__(cfg, logger)
        self.path = cfg.get('battmon', 'sysfs_voltage_path')
        if not self.path:
            raise ValueError("No sysfs_voltage_path configured for battmon_sysfs")

    def init_batt_mon_hw(self, num, denom):
        """Initialize battery monitor with scaling factor for scaling raw volatage readings to mV.

        :param num: Numerator of the scaling factor
        :type num: int or float
        :param denom: Denominator of the scaling factor
        :type denom: int or float
        :raises ZeroDivisionError: If denom is zero
        """
        self.scale = num / denom

    def get_batt_voltage(self):
        with open(self.path, 'r') as f:
            raw_value = float(f.read().strip())
        voltage = int(raw_value * self.scale)
        return voltage


@plugs.finalize
def finalize():
    global batt_mon
    cfg = jukebox.cfghandler.get_handler('jukebox')
    batt_mon = battmon_sysfs(cfg)
    plugs.register(batt_mon, name='batt_mon')


@plugs.atexit
def atexit(**ignored_kwargs):
    global batt_mon
    batt_mon.status_thread.cancel()
