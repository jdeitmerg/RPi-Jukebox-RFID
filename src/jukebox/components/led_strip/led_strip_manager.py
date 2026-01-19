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
    def __init__(self, num_leds=16, pin=12, brightness=50, base_color=(255, 255, 255)):
        super().__init__(name='LedStripManager')
        self._keep_running = True
        self.daemon_proc = None
        self.num_leds = num_leds
        self.pin = pin
        self.brightness = brightness
        self.base_color = dict(zip(['r', 'g', 'b'], base_color))
        self.lock = threading.Lock()
        self.daemon_socket = None
        self.ts_start = time.monotonic()

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

    def _start_daemon(self):
        self.daemon_proc = subprocess.Popen(['sudo', f'{DAEMON_DIR}/run_daemon.sh',
                                              '--num-leds', str(self.num_leds),
                                              '--pin', str(self.pin),
                                              '--brightness', str(self.brightness),
                                              '--base-color', ','.join(str(v) for v in self.base_color.values())],
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

            self._update_daemon()

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
        self._update_daemon()

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

    def _update_daemon(self):
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
        self._update_daemon()

    def stop(self):
        self._keep_running = False
        if self.daemon_proc:
            logger.debug('Requesting LED daemon shutdown...')
            # Can't use terminate(), as sudo was used to start the process, which creates a new process group.
            self._rpc_call('exit')
            self.daemon_proc.wait()
            logger.debug('LED daemon process terminated.')
        self.daemon_socket.close()
        self.context.term()
