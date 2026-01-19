import threading
import time
import logging
from enum import Enum
import json
import zmq
import subprocess
import jukebox.publishing.subscriber as subscriber
import jukebox.cfghandler
import jukebox.plugs as plugin
from datetime import datetime

logger = logging.getLogger('jb.led_strip.manager')
cfg = jukebox.cfghandler.get_handler('jukebox')

DAEMON_DIR = __file__.replace('led_strip_manager.py', 'daemon')
SOCKET_PATH = '/tmp/led_strip_daemon.sock'


class LedState(Enum):
    # State IDs are also their priority (higher number = higher priority)
    IDLE = 10
    CHARGING = 20
    FULL = 25
    SYNC = 30
    BATT_WARNING = 40
    OVERLAY = 50  # Temporary overlays like volume/battery
    READY = 60  # Short animation on startup
    SHUTDOWN = 100


class LedStripManager(threading.Thread):
    def __init__(self, num_leds=16, pin=12, brightness=20, base_color=(255, 255, 255), reverse_direction=False,
                 nightmode_times=None, nightmode_brightness=5):
        ''' Manages the LED strip daemon process and communicates with it via RPC.
        Args:
            num_leds (int): Number of LEDs in the strip.
            pin (int): GPIO pin connected to the LED strip.
            brightness (int): Brightness level (0-100).
            base_color (tuple): Base color as (r, g, b).
            reverse_direction (bool): Whether to reverse the LED strip direction.
            nightmode_times (tuple): Optional tuple of (start_time, end_time) for night
                mode. Both need to be of type datetime.time.
            nightmode_brightness (int): Brightness level (0-100) during night mode.
        '''
        logger.debug('Initializing LedStripManager with parameters: '
                     f'num_leds={num_leds}, pin={pin}, brightness={brightness}, '
                     f'base_color={base_color}, reverse_direction={reverse_direction}, '
                     f'nightmode_times={nightmode_times}, nightmode_brightness={nightmode_brightness}')
        super().__init__(name='LedStripManager')
        self._keep_running = True
        self.daemon_proc = None
        self.num_leds = num_leds
        self.pin = pin
        self.base_color = dict(zip(['r', 'g', 'b'], base_color))
        self.lock = threading.Lock()
        self.daemon_socket = None
        self.reverse_direction = reverse_direction
        self.ts_start = time.monotonic()
        self.nightmode_times = nightmode_times
        self.nightmode_brightness = nightmode_brightness
        self.daymode_brightness = brightness
        self.nightmode = self._is_night()
        self.brightness = nightmode_brightness if self.nightmode else brightness

        with self.lock:
            self._start_daemon()
            # RPC Setup
            self.context = zmq.Context()
            for _ in range(20):
                try:
                    self._socket_connect()
                    if self._poll_daemon():  # takes up to 500ms
                        break
                    time.sleep(0.5)
                except Exception:
                    pass
            else:
                raise RuntimeError('LED Daemon did not start properly!')
        logger.info('Connected to LED Strip Daemon')

        # State tracking
        self.layers = {LedState.IDLE}  # Stack of active layers
        self.layer_data = {}
        # State ID and associated data that is currently being displayed. Daemon auto-initiates to idle, no need to send it.
        self.current_state = (LedState.IDLE, None)
        self.overlay_timeout = 0
        self.overlay_start_time = 0

        # Subscriptions
        self.sub = subscriber.Subscriber('inproc://PublisherToProxy', [
            'volume.level', 'sync.status', 'batt_status'
        ])

    def _is_night(self):
        if not self.nightmode_times:
            return False
        now = datetime.now().time()
        night_start, night_end = self.nightmode_times
        return now >= night_start or now < night_end

    def _update_nightmode(self):
        if not self.nightmode_times:
            return
        nightmode = self._is_night()
        if nightmode != self.nightmode:
            self.nightmode = nightmode
            self.brightness = self.nightmode_brightness if nightmode else self.daymode_brightness
            logger.info(f'Night mode {"enabled" if nightmode else "disabled"}, fading brightness to {self.brightness}')
            self._rpc_call('start_fade', {'brightness': self.brightness})

    def _start_daemon(self):
        args = ['sudo', f'{DAEMON_DIR}/run_daemon.sh',
                '--num-leds', str(self.num_leds),
                '--pin', str(self.pin),
                '--brightness', str(self.brightness),
                '--base-color', ','.join(str(v) for v in self.base_color.values())]
        if self.reverse_direction:
            args.append('--reverse_direction')
        self.daemon_proc = subprocess.Popen(args,
                                            stdout=subprocess.DEVNULL,  # Suppress output to avoid cluttering logs
                                            stderr=subprocess.DEVNULL,
                                            # start_new_session makes sure the process is not killed immediately when the
                                            # main app receives SIGINT/SIGTERM. It also stops the log from getting messed up
                                            # (looking like carriage return missing on Windows).
                                            start_new_session=True)

    def _poll_daemon(self):
        # Check if daemon is running
        try:
            logger.debug('Pinging LED daemon...')
            self.daemon_socket.send_string(json.dumps({'method': 'ping'}))
            self.daemon_socket.recv_string()
            return True
        except Exception:
            return False

    def _socket_connect(self):
        if self.daemon_socket:
            self.daemon_socket.close()
        self.daemon_socket = self.context.socket(zmq.REQ)
        logger.debug(f'Connecting to LED daemon socket at ipc://{SOCKET_PATH}...')
        self.daemon_socket.connect(f'ipc://{SOCKET_PATH}')
        self.daemon_socket.setsockopt(zmq.LINGER, 500)
        self.daemon_socket.setsockopt(zmq.RCVTIMEO, 500)

    def _rpc_call(self, method, params=None):
        with self.lock:
            try:
                self.daemon_socket.send_string(json.dumps({'method': method, 'params': params or {}}))
                self.daemon_socket.recv_string()  # Wait for ack
            except Exception as e:
                logger.error(f'Failed to call LED daemon: {e}')
                # Reconnect on error
                self._socket_connect()
                assert self._poll_daemon(), 'Unable to reconnect to LED daemon!'

    def run(self):
        logger.info('LedStripManager started')

        # Trigger Ready animation
        self.layers.add(LedState.READY)
        self.overlay_start_time = time.monotonic()

        while self._keep_running:
            try:
                topic, payload = self.sub.receive()  # Blocking with timeout of 500ms
                self._handle_event(topic, payload)
            except zmq.Again:
                pass  # No request received

            # Check timeouts for Ready and Overlays
            now = time.monotonic()
            if LedState.OVERLAY in self.layers and now - self.overlay_start_time > self.overlay_timeout:
                self.layers.remove(LedState.OVERLAY)

            if LedState.READY in self.layers and now - self.overlay_start_time > 3.0:
                self.layers.remove(LedState.READY)

            self._update_nightmode()

            self._update_daemon_state()

    def _handle_event(self, topic, payload):
        logger.debug(f'Received event on topic "{topic}": {payload}')
        match topic:
            case 'volume.level':
                if time.monotonic() - self.ts_start < 10:
                    # The volume is always set automatically on startup. Supress showing the volume bar.
                    logger.debug('Not displaying volume change right after startup')
                else:
                    max_volume = plugin.call('volume', 'ctrl', 'get_soft_max_volume')
                    self._trigger_overlay('volume', payload['volume'] / max_volume, duration=5)
            case 'sync.status':
                self._handle_sync(payload)
            case 'batt_status':
                self._handle_battery(payload)
            case _:
                logger.warning(f'Unhandled topic "{topic}" in LedStripManager')

    def _trigger_overlay(self, type, value, duration):
        self.layers.add(LedState.OVERLAY)
        self.layer_data[LedState.OVERLAY] = {'type': type, 'value': value}
        self.overlay_timeout = duration
        self.overlay_start_time = time.monotonic()
        # Immediately update daemon to show overlay, as this can be called from outside the run() loop
        self._update_daemon_state()

    def _handle_battery(self, payload):
        soc = payload.get('soc', 0) / 100
        warning = soc < .2
        charging = payload.get('charging', 0)
        full = soc >= .99

        self.layers.discard(LedState.BATT_WARNING)
        self.layers.discard(LedState.CHARGING)
        self.layers.discard(LedState.FULL)

        if warning:
            self.layers.add(LedState.BATT_WARNING)
            self.layer_data[LedState.BATT_WARNING] = {'soc': soc}
        elif full:
            self.layers.add(LedState.FULL)
        elif charging:
            self.layers.add(LedState.CHARGING)
            self.layer_data[LedState.CHARGING] = {'soc': soc}

    def _handle_sync(self, payload):
        active = payload.get('active', False)
        if active:
            self.layers.add(LedState.SYNC)
        else:
            self.layers.discard(LedState.SYNC)

    def _update_daemon_state(self):
        show_state = max(self.layers, key=lambda s: s.value)
        show_state_data = self.layer_data.get(show_state)
        if (show_state, show_state_data) == self.current_state:
            return  # No change

        match show_state:
            case LedState.IDLE:
                self._rpc_call('set_solid', self.base_color)
            case LedState.CHARGING:
                self._rpc_call('start_animation', {'name': 'charging', 'data': show_state_data})
            case LedState.FULL:
                self._rpc_call('set_battery', {'ratio': 1})
            case LedState.SYNC:
                self._rpc_call('start_animation', {'name': 'sync'})
            case LedState.BATT_WARNING:
                self._rpc_call('set_battery', {'ratio': show_state_data['soc']})
            case LedState.OVERLAY:
                if show_state_data['type'] == 'volume':
                    self._rpc_call('set_bar', {'ratio': show_state_data['value']})
                elif show_state_data['type'] == 'battery':
                    # Battery level is static bar in daemon
                    self._rpc_call('set_battery', {'ratio': show_state_data['value']})
            case LedState.READY:
                self._rpc_call('start_animation', {'name': 'ready'})
            case LedState.SHUTDOWN:
                self._rpc_call('start_animation', {'name': 'shutdown'})
            case _:
                logger.warning(f'Unhandled LED state: {show_state}')

        self.current_state = (show_state, show_state_data)

    def trigger_shutdown(self):
        self.layers.add(LedState.SHUTDOWN)
        # Immediately update daemon to show animation, as this can be called from outside the run() loop
        self._update_daemon_state()

    def stop(self):
        self._keep_running = False
        if self.daemon_proc:
            logger.debug('Requesting LED daemon shutdown')
            # Can't use terminate(), as sudo was used to start the process, which creates a new process group.
            self._rpc_call('exit')
            # Don't wait for process to exit so we're not killed waiting. Better to let it shut down at its own pace.
        self.daemon_socket.close()
        self.context.term()
