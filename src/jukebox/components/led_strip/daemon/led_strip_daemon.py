#!/usr/bin/env python3
import argparse
import grp
import json
import logging
import os
import threading
import time
import zmq
from rpi_ws281x import Color, PixelStrip

SOCKET_PATH = '/run/jukebox/led_strip_daemon.sock'
SOCKET_GROUP_ENV = 'JUKEBOX_LED_STRIP_GROUP'
DEFAULT_SOCKET_GROUP = 'users'
PIXELS_PER_LED = 20  # Good value for smooth animations, see VirtualPixelStrip class docstring for details
ANIMATION_TIMEOUT_MS = 50  # 20 FPS
IDLE_TIMEOUT_MS = 10000  # Update LEDs every 10s when idle to fix potential corruption
FADE_DURATION = 2.0  # Brightness fade duration in seconds

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('led_strip_daemon')


class VirtualPixelStrip(PixelStrip):
    ''' A virtual pixel strip that uses multiple virtual pixels per real LED for smoother animations.
        Also adds reversing functionality.
        The default of 20 pixels per LED was chosen so in the scenario of a pattern moving across 2 LEDs per second, a given
        LED can change color up to 40 times per second. Otherwise even at 20 FPS there's a noticeable difference in smoothness.
    '''
    def __init__(self, num, pin, freq_hz=800000, dma=10, invert=False,
                 brightness=255, channel=0, strip_type=None, gamma=None, pixels_per_led=20, reverse=False):
        super().__init__(num, pin, freq_hz, dma, invert, brightness, channel, strip_type, gamma)
        self.num_leds = num
        self.pixels_per_led = pixels_per_led
        self.pixels = [Color(0, 0, 0)] * (num * pixels_per_led)
        self.reverse = reverse

    def __getitem__(self, index):
        return self.pixels[index]

    def __setitem__(self, index, color):
        ''' Set the color value at the provided position or slice of positions.
        '''
        if isinstance(index, slice):
            for i in range(*index.indices(len(self.pixels))):
                self.pixels[i] = color
        else:
            self.pixels[index] = color

    def __len__(self):
        return len(self.pixels)

    def _downsample(self):
        ''' Downsample virtual pixels to real LEDs by averaging colors.
            Reverse order if needed.
        '''
        for i in range(self.num_leds):
            r_total, g_total, b_total = 0, 0, 0
            for j in range(self.pixels_per_led):
                color = self.pixels[i * self.pixels_per_led + j]
                r_total += color.r
                g_total += color.g
                b_total += color.b
            r_avg = int(r_total / self.pixels_per_led)
            g_avg = int(g_total / self.pixels_per_led)
            b_avg = int(b_total / self.pixels_per_led)
            target_idx = self.num_leds - 1 - i if self.reverse else i
            super().__setitem__(target_idx, Color(r_avg, g_avg, b_avg))

    def show(self):
        self._downsample()
        super().show()


def clamp_ratio(ratio):
    return max(0.0, min(1.0, ratio))


class LedManager:
    def __init__(self, num_leds, pin, base_color, brightness=50, reverse_direction=False):
        self.animation_cb = None
        self.anim_start_time = 0
        self.anim_data = {}
        self.fade_start_time = None
        self.fade_start_brightness = None
        self.fade_target_brightness = None
        self.lock = threading.Lock()
        self.strip = VirtualPixelStrip(num_leds, pin, brightness=brightness, pixels_per_led=PIXELS_PER_LED,
                                       reverse=reverse_direction)
        self.num_pixels = len(self.strip)
        self.strip.begin()
        self.base_color = Color(base_color['r'], base_color['g'], base_color['b'])
        self._init_lookup_tables()

        logger.info(f"LED Daemon initialized on pin {pin} with {num_leds} LEDs, brightness {brightness}/255, "
                    f"reverse_direction={reverse_direction}")

    def animation_active(self):
        return self.animation_cb is not None or self.fade_start_time is not None

    def _init_lookup_tables(self):
        self._battery_lookup = [Color(255, 0, 0)] * int(self.num_pixels * 0.2)  # red up to 20%
        self._battery_lookup += [Color(255, 255, 0)] * int(self.num_pixels * 0.2)  # yellow up to 40%
        remaining_pixels = self.num_pixels - len(self._battery_lookup)
        self._battery_lookup += [Color(0, 255, 0)] * remaining_pixels  # green up to 100%

    def _clear(self) -> None:
        self.strip[:] = Color(0, 0, 0)

    def set_solid(self, r, g, b):
        with self.lock:
            self.animation_cb = None
            color = Color(r, g, b)
            self.strip[:] = color
            self.strip.show()
            logger.debug(f"Set solid color: ({r}, {g}, {b})")

    def set_bar(self, ratio, r, g, b):
        with self.lock:
            self.animation_cb = None
            ratio = clamp_ratio(ratio)
            num_lit = int(ratio * self.num_pixels)
            color = Color(r, g, b)
            if num_lit:
                self.strip[:num_lit] = color
            if num_lit < self.num_pixels:
                self.strip[num_lit:] = Color(0, 0, 0)
            self.strip.show()
            logger.debug(f"Set bar: {ratio * 100}%")

    def set_battery_bar(self, ratio):
        with self.lock:
            self.animation_cb = None
            self._render_battery_bar(ratio)
            self.strip.show()
            logger.debug(f"Set battery bar: {ratio * 100}%")

    def start_animation(self, name, data=None):
        with self.lock:
            animation_cb = None
            match name:
                case 'ready':
                    animation_cb = self._pattern_ready_cb
                case 'sync':
                    animation_cb = self._pattern_sync_cb
                case 'shutdown':
                    animation_cb = self._pattern_shutdown_cb
                case 'charging':
                    animation_cb = self._pattern_charging_cb
            if self.animation_cb != animation_cb:
                # Only restart if different animation. Otherwise only update data.
                self.anim_start_time = time.monotonic()
            self.animation_cb = animation_cb
            self.anim_data = data or {}
            logger.info(f"Started animation: {name}")

    def start_fade(self, target_brightness):
        self.fade_start_time = time.monotonic()
        self.fade_target_brightness = target_brightness
        self.fade_start_brightness = self.strip.getBrightness()
        logger.debug(f"Started fade to brightness: {target_brightness}/255")

    def update_animation(self):
        with self.lock:
            if self.animation_cb is not None:
                elapsed = time.monotonic() - self.anim_start_time
                self.animation_cb(elapsed)

            self._fade()

            self.strip.show()

    def _pattern_ready_cb(self, elapsed):
        # First 1.5s: light up from center outwards. Then two quick pulses, ending at full brightness.
        center = self.num_pixels / 2.0
        if elapsed < 1.5:
            # Light up bar from center outwards
            progress = elapsed / 1.5
            pattern_start = int(center - (progress * (self.num_pixels / 2.0)))
            pattern_end = int(center + (progress * (self.num_pixels / 2.0)))
            self.strip[:pattern_start] = Color(0, 0, 0)
            self.strip[pattern_start:pattern_end] = self.base_color
            self.strip[pattern_end:] = Color(0, 0, 0)
        else:
            # Quick pulses
            if (elapsed - 1.5) > 2.1:
                self.strip[:] = self.base_color
            else:
                sub_elapsed = (elapsed - 1.5) % 0.7
                brightness = abs(0.35 - sub_elapsed) / 0.35
                # Scale colors instead of setting brightness directly to avoid having to reset the brightness later
                r = int(self.base_color.r * brightness)
                g = int(self.base_color.g * brightness)
                b = int(self.base_color.b * brightness)
                self.strip[:] = Color(r, g, b)

    def _pattern_sync_cb(self, elapsed):
        # Moving dots (10% of pixels) from edges to center and back
        self._clear()
        period = 2.0
        progress = (elapsed % period) / period
        dot_width = int(self.num_pixels * 0.1)
        # Calculate start index based on progress. Limit to range that makes dots collide but not "cross".
        start_idx = int(abs(0.5 - progress) * (self.num_pixels - dot_width))
        self.strip[start_idx:(start_idx + dot_width)] = self.base_color
        self.strip[(-start_idx - 1):(-start_idx - dot_width - 1): -1] = self.base_color

    def _pattern_charging_cb(self, elapsed):
        soc = self.anim_data.get('soc', 0)
        # Moving bar indicating charge level
        animation_period = 3.0
        total_period = animation_period + 1.0  # 1s constant at the end
        progress = min(1, (elapsed % total_period) / animation_period)
        # Stop fill-up animation at current SOC
        self._render_battery_bar(min(soc, progress))

    def _render_battery_bar(self, ratio):
        ratio = clamp_ratio(ratio)
        num_lit = int(ratio * self.num_pixels)

        if num_lit:
            self.strip.pixels[:num_lit] = self._battery_lookup[:num_lit]
        if num_lit < self.num_pixels:
            self.strip[num_lit:] = Color(0, 0, 0)

    def _pattern_shutdown_cb(self, elapsed):
        # Light down from edges to center. Leave 10% of pixels in the center on until power is cut
        duration = 2.0
        progress = min(elapsed / duration, 1.0)
        center = self.num_pixels / 2.0
        pattern_width = max(0.1, 1.0 - progress) * self.num_pixels
        pattern_start = int(center - (pattern_width / 2))
        pattern_end = int(center + (pattern_width / 2))
        self.strip[:pattern_start] = Color(0, 0, 0)
        self.strip[pattern_start:pattern_end] = self.base_color
        self.strip[pattern_end:] = Color(0, 0, 0)

    def _fade(self):
        if self.fade_start_time is None:
            return

        elapsed = time.monotonic() - self.fade_start_time
        if elapsed >= FADE_DURATION:
            brightness = self.fade_target_brightness
            self.fade_start_time = None
            logger.debug(f"Fade completed to brightness: {brightness}/255")
        else:
            offset = elapsed / FADE_DURATION * (self.fade_target_brightness - self.fade_start_brightness)
            brightness = self.fade_start_brightness + offset

        self.strip.setBrightness(max(0, min(255, int(brightness))))


class MsgHandler:
    def __init__(self, socket_group):
        self.led_mgr = None
        self.socket_group = socket_group

    def __enter__(self):
        socket_dir = os.path.dirname(SOCKET_PATH)
        os.makedirs(socket_dir, exist_ok=True)
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind(f'ipc://{SOCKET_PATH}')
        # Make sure jukebox-daemon can access the newly created socket.
        group = grp.getgrnam(self.socket_group).gr_gid
        os.chown(SOCKET_PATH, 0, group)  # 0 is root uid
        os.chmod(SOCKET_PATH, 0o660)     # Ensure it's readable/writable by the group
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.socket.close()
        self.context.term()

    def _configure(self, params):
        base_color = params.get('base_color', {'r': 255, 'g': 255, 'b': 255})
        if isinstance(base_color, list):
            base_color = dict(zip(['r', 'g', 'b'], base_color))

        self.led_mgr = LedManager(
            num_leds=int(params.get('num_leds', 16)),
            pin=int(params.get('pin', 12)),
            brightness=int(int(params.get('brightness', 20)) / 100 * 255),
            base_color={
                'r': int(base_color.get('r', 255)),
                'g': int(base_color.get('g', 255)),
                'b': int(base_color.get('b', 255)),
            },
            reverse_direction=bool(params.get('reverse_direction', False))
        )

    def _requires_configuration(self, method):
        return method not in {'configure', 'ping'}

    def _process_request(self, request):
        logger.debug(f"Received request: {request}")
        method = request.get('method')
        params = request.get('params', {})

        logger.info(f"Processing method: {method} with params: {params}")

        if self.led_mgr is None and self._requires_configuration(method):
            logger.warning(f"LED Daemon is not configured yet, ignoring method: {method}")
        else:
            match method:
                case 'configure':
                    self._configure(params)
                case 'set_solid':
                    self.led_mgr.set_solid(params.get('r', 0), params.get('g', 0), params.get('b', 0))
                case 'set_bar':
                    self.led_mgr.set_bar(params.get('ratio', 0),
                                         params.get('r', 255),
                                         params.get('g', 255),
                                         params.get('b', 255))
                case 'set_battery':
                    self.led_mgr.set_battery_bar(params.get('ratio', 0))
                case 'start_animation':
                    self.led_mgr.start_animation(params.get('name'), params.get('data'))
                case 'stop_animation':
                    self.led_mgr.animation_cb = None
                case 'start_fade':
                    self.led_mgr.start_fade(int(params.get('brightness', 50) / 100 * 255))
                case 'ping':
                    pass  # Just respond with 'ok'
                case 'exit':
                    logger.info("Ignoring exit request; systemd owns the LED daemon lifecycle")
                case _:
                    logger.warning(f"Unknown method: {method}")

        self.socket.send_string(json.dumps({'status': 'ok'}))

    def receive_and_process(self):
        # Adjust sleep time (via socket timeout) based on whether an animation is active
        if self.led_mgr and self.led_mgr.animation_active():
            # Timeout determines the update rate during animations
            self.socket.setsockopt(zmq.RCVTIMEO, ANIMATION_TIMEOUT_MS)
        else:
            # Larger timeout when idle, only for updating LEDs in case they are corrupted
            self.socket.setsockopt(zmq.RCVTIMEO, IDLE_TIMEOUT_MS)
        try:
            message = self.socket.recv_string()
            request = json.loads(message)
            self._process_request(request)
        except zmq.Again:
            pass  # No request received
        if self.led_mgr:
            self.led_mgr.update_animation()


def main():
    parser = argparse.ArgumentParser(description="LED Strip Daemon")
    parser.add_argument("--socket-group", default=os.environ.get(SOCKET_GROUP_ENV, DEFAULT_SOCKET_GROUP),
                        help="Group allowed to access the IPC socket")
    args = parser.parse_args()

    with MsgHandler(args.socket_group) as handler:
        logger.info(f"LED Daemon listening on IPC socket {SOCKET_PATH}")
        while True:
            handler.receive_and_process()
            # Handler takes care of timing via socket timeout, no need to sleep here
    # Don't clear on exit to keep last state visible


if __name__ == "__main__":
    main()
