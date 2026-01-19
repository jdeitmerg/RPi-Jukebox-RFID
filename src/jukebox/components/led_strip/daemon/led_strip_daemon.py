#!/usr/bin/env python3
import time
import zmq
import json
import logging
import threading
import argparse
import os
import grp
from rpi_ws281x import Color, PixelStrip

SOCKET_PATH = '/tmp/led_strip_daemon.sock'

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('led_strip_daemon')


class VirtualPixelStrip(PixelStrip):
    ''' A virtual pixel strip that uses multiple virtual pixels per real LED for smoother animations.'''
    def __init__(self, num, pin, freq_hz=800000, dma=10, invert=False,
                 brightness=255, channel=0, strip_type=None, gamma=None, pixels_per_led=10):
        super().__init__(num, pin, freq_hz, dma, invert, brightness, channel, strip_type, gamma)
        self.num_leds = num
        self.pixels_per_led = pixels_per_led
        self.pixels = [Color(0, 0, 0)] * (num * pixels_per_led)

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
        ''' Downsample virtual pixels to real LEDs by averaging colors. '''
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
            super().__setitem__(i, Color(r_avg, g_avg, b_avg))

    def show(self):
        self._downsample()
        super().show()


class LedManager:
    def __init__(self, num_leds, pin, base_color, brightness=50):
        self.cur_animation = None
        self.anim_start_time = 0
        self.anim_data = {}
        self.lock = threading.Lock()
        self.strip = VirtualPixelStrip(num_leds, pin, brightness=brightness, pixels_per_led=20)
        self.num_pixels = len(self.strip)
        self.strip.begin()
        self.base_color = Color(base_color['r'], base_color['g'], base_color['b'])
        self._init_lookup_tables()

        logger.info(f"LED Daemon initialized on pin {pin} with {num_leds} LEDs")

    def _init_lookup_tables(self):
        self._battery_lookup = []
        for i in range(self.num_pixels):
            ratio = (i + 1) / self.num_pixels
            if ratio <= 0.2:
                color = Color(255, 0, 0)
            elif ratio <= 0.4:
                color = Color(255, 255, 0)
            else:
                color = Color(0, 255, 0)
            self._battery_lookup.append(color)

    def set_solid(self, r, g, b):
        with self.lock:
            self.cur_animation = None
            color = Color(r, g, b)
            for i in range(self.num_pixels):
                self.strip.setPixelColor(i, color)
            self.strip.show()
            logger.debug(f"Set solid color: ({r}, {g}, {b})")

    def set_bar(self, ratio, r, g, b):
        with self.lock:
            self.cur_animation = None
            num_lit = int(ratio * self.num_pixels)
            # One LED might not be fully lit to create a smoother effect
            partial_brightness = ratio * self.num_pixels - num_lit
            color = Color(r, g, b)
            for i in range(num_lit):
                self.strip.setPixelColor(i, color)
            if num_lit < self.num_pixels:
                r_c = int(((color >> 16) & 0xFF) * partial_brightness)
                g_c = int(((color >> 8) & 0xFF) * partial_brightness)
                b_c = int((color & 0xFF) * partial_brightness)
                self.strip.setPixelColor(num_lit, Color(r_c, g_c, b_c))
            for i in range(num_lit + 1, self.num_pixels):
                self.strip.setPixelColor(i, Color(0, 0, 0))
            self.strip.show()
            logger.debug(f"Set bar: {ratio * 100}%")

    def set_battery_bar(self, ratio):
        with self.lock:
            self.cur_animation = None
            self._render_battery_bar(ratio)
            self.strip.show()
            logger.debug(f"Set battery bar: {ratio * 100}%")

    def start_animation(self, name, data=None):
        with self.lock:
            self.cur_animation = name
            self.anim_start_time = time.monotonic()
            self.anim_data = data or {}
            logger.info(f"Started animation: {name}")

    def update_animation(self):
        if not self.cur_animation:
            return
        with self.lock:
            elapsed = time.monotonic() - self.anim_start_time

            match self.cur_animation:
                case 'ready':
                    self._pattern_ready(elapsed)
                case 'sync':
                    self._pattern_sync(elapsed)
                case 'shutdown':
                    self._pattern_shutdown(elapsed)
                case 'charging':
                    self._pattern_charging(elapsed, self.anim_data.get('soc', 0))

            self.strip.show()

    def _pattern_ready(self, elapsed):
        # First 1.5s: light up from center outwards. Then two quick pulses.
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
            sub_elapsed = (elapsed - 1.5) % 0.7
            brightness = abs(0.35 - sub_elapsed) / 0.35
            # Scale colors instead of setting brightness directly to avoid having to reset the brightness later
            r = int(self.base_color.r * brightness)
            g = int(self.base_color.g * brightness)
            b = int(self.base_color.b * brightness)
            self.strip[:] = Color(r, g, b)

    def _pattern_sync(self, elapsed):
        # Moving dots (10% of pixels) from edges to center and back
        self.strip[:] = Color(0, 0, 0)
        period = 2.0
        progress = (elapsed % period) / period
        dot_width = int(self.num_pixels * 0.1)
        # Calculate start index based on progress. Limit to range that makes dots collide but not "cross".
        start_idx = int(abs(0.5 - progress) * (self.num_pixels - dot_width))
        self.strip[start_idx:(start_idx + dot_width)] = self.base_color
        self.strip[(-start_idx - 1):(-start_idx - dot_width - 1): -1] = self.base_color

    def _pattern_charging(self, elapsed, soc):
        # Moving bar indicating charge level
        animation_period = 3.0
        total_period = animation_period + 1.0  # 1s constant at the end
        progress = min(1, (elapsed % total_period) / animation_period)
        # Stop fill-up animation at current SOC
        self._render_battery_bar(min(soc, progress))

    def _render_battery_bar(self, ratio):
        ratio = max(0.0, min(1.0, ratio))
        num_lit = int(ratio * self.num_pixels)

        self.strip.pixels[:num_lit] = self._battery_lookup[:num_lit]
        self.strip[num_lit:] = Color(0, 0, 0)

    def _pattern_shutdown(self, elapsed):
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


class MsgHandler():
    def __init__(self, led_mgr):
        self.led_mgr = led_mgr

    def __enter__(self):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind(f'ipc://{SOCKET_PATH}')
        self.socket.setsockopt(zmq.RCVTIMEO, 50)  # 50ms timeout for non-blocking receive -> 20 FPS update rate
        # Make sure users other than root can access the newly created socket
        group = grp.getgrnam('users').gr_gid
        os.chown(SOCKET_PATH, 0, group)  # 0 is root uid
        os.chmod(SOCKET_PATH, 0o660)     # Ensure it's readable/writable by the group
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.socket.close()
        self.context.term()

    def _process_request(self, request):
        logger.debug(f"Received request: {request}")
        method = request.get('method')
        params = request.get('params', {})

        logger.info(f"Processing method: {method} with params: {params}")

        do_exit = False
        match method:
            case 'set_solid':
                self.led_mgr.set_solid(params.get('r', 0), params.get('g', 0), params.get('b', 0))
            case'set_bar':
                self.led_mgr.set_bar(params.get('ratio', 0),
                                    params.get('r', 255),
                                    params.get('g', 255),
                                    params.get('b', 255))
            case 'set_battery':
                self.led_mgr.set_battery_bar(params.get('ratio', 0))
            case 'start_animation':
                self.led_mgr.start_animation(params.get('name'), params.get('data'))
            case 'stop_animation':
                self.led_mgr.cur_animation = None
            case 'ping':
                pass  # Just respond with 'ok'
            case 'exit':
                logger.info("Shutting down LED Daemon as per request")
                do_exit = True
            case _:
                logger.warning(f"Unknown method: {method}")

        self.socket.send_string(json.dumps({'status': 'ok'}))
        if do_exit:
            exit(0)

    def receive_and_process(self):
        try:
            message = self.socket.recv_string()
            request = json.loads(message)
            self._process_request(request)
        except zmq.Again:
            pass  # No request received
        self.led_mgr.update_animation()


def main():
    parser = argparse.ArgumentParser(description="LED Strip Daemon")
    parser.add_argument("--pin", type=int, default=12, help="GPIO pin to which the LED strip is connected")
    parser.add_argument("--num-leds", type=int, default=16, help="Number of LEDs in the strip")
    parser.add_argument("--brightness", type=int, default=50, help="Brightness of the LED strip (0-100)")
    parser.add_argument("--base-color", type=str, default="255,255,255", help="Base color in R,G,B format")
    args = parser.parse_args()

    led_mgr = LedManager(
        num_leds=args.num_leds,
        pin=args.pin,
        brightness=args.brightness,
        base_color=dict(zip(['r', 'g', 'b'], map(int, args.base_color.split(','))))
    )

    with MsgHandler(led_mgr) as handler:
        logger.info("LED Daemon listening on IPC socket")
        while True:
            handler.receive_and_process()
            # Handler takes care of timing via socket timeout, no need to sleep here
    # Don't clear on exit to keep last state visible


if __name__ == "__main__":
    main()
