#!/usr/bin/env python3
import configparser
import string
import sys
import os
import signal
import logging
import argparse
from pathlib import Path
import random
from queue import Queue
from threading import Thread
import queue
from paho.mqtt import client as mqtt_client
from  paho.mqtt.client import MQTTMessage as mqtt_message
import time
from ampyentika.ambientika import Modes, HumLevels, VentLevels, VentLevelModes, HumLevelModes, NonPersistentModes
from ampyentika.homeassistant import homeassistant_discover


class Ampyentika:
    def __init__(self, config_path, loglevel=logging.INFO):
            self.setup_logging(loglevel)
            signal.signal(signal.SIGTERM, self.handle_signal)
            signal.signal(signal.SIGINT, self.handle_signal)

            self.config_path = config_path

            self.mode = None
            self.humLevel = None
            self.ventLevel = None
            self.last_command = time.time()
            self.command_interval = 1

            self.config = self.parse_config(config_path)

            self.inbound_queue: "Queue[mqtt_message]"= queue.Queue()
            self.outbound_queue: "Queue[dict]" = queue.Queue()


            self.mqtt_receive_client = self._connect_mqtt(
                client_id=f"{self.config["General"]["name"].lower()}-receive-"
                          f"{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}",
                username=self.config["MQTT"]["username"],
                password=self.config["MQTT"]["password"],
                host=self.config["MQTT"]["host"],
                port=int(self.config["MQTT"]["port"]))
            self.mqtt_send_client = self._connect_mqtt(
                client_id=f"{self.config["General"]["name"].lower()}-send-"
                          f"{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}",
                username=self.config["MQTT"]["username"],
                password=self.config["MQTT"]["password"],
                host=self.config["MQTT"]["host"],
                port=int(self.config["MQTT"]["port"]))
            self._subscribe_mqtt([f"{self.config["MQTT"]["topic"]}/#"])

            self.mqtt_loop_thread = Thread(target=self.mqtt_receive_client.loop_forever, daemon=True)
            self.mqtt_loop_thread.start()

            homeassistant_discover(self.config["General"]["name"].lower(),
                                   self.config["General"]["unique_id"].lower(),
                                   self.config["MQTT"]["topic"], self.mqtt_receive_client)

    @staticmethod
    def _connect_mqtt(client_id, username, password, host, port):
        def on_connect(client, userdata, flags, rc, properties=None):
            if rc == 0:
                logging.info("Connected to MQTT Broker!")
            else:
                logging.debug(f"Failed to connect, return code {rc}")

        client = mqtt_client.Client(client_id=client_id,
                                    callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2)

        client.username_pw_set(username, password)
        client.on_connect = on_connect
        client.connect(host, port)
        return client

    def _subscribe_mqtt(self, topics=()):
        for topic in topics:
            self.mqtt_receive_client.subscribe(topic)
            logging.info(f"Subscribed to {topic}")

        self.mqtt_receive_client.on_message = self.on_message

    def on_message(self, client, userdata, message):
        logging.debug(f"Received message {message.payload.decode()}")
        self.inbound_queue.put(message)

    def message_worker(self):
        while True:
            self.mqtt_send_client.loop()
            message = self.inbound_queue.get()
            topic = Path(message.topic)

            if topic.stem == "set":
                payload = message.payload.decode()
                logging.debug(f"Received message {payload} in {topic}")

                if topic.parent.stem == "mode":
                    logging.debug(f"Processing mode")
                    mode_key = payload
                    if mode_key in Modes:
                        self.set_mode(mode_key)
                        time.sleep(self.command_interval)
                        if self.mode in VentLevelModes:
                            self.set_level(self.ventLevel)
                        elif self.mode in HumLevelModes:
                            self.set_level(self.humLevel)
                            time.sleep(self.command_interval)

                elif topic.parent.stem == "level":
                    logging.debug(f"Processing level")
                    level_key = int(payload)
                    if ((self.mode in VentLevelModes and level_key in VentLevels) or
                            (self.mode in HumLevelModes and level_key in HumLevels)):
                        self.set_mode(self.mode)
                        time.sleep(self.command_interval)
                        self.set_level(level_key)
                        time.sleep(self.command_interval)
                    else:
                        logging.debug(f"{level_key} not known for mode {self.mode}")

            self.inbound_queue.task_done()

    def set_level(self, level_key):
        logging.warning(f"Setting {level_key} for mode {self.mode}")
        if self.mode in VentLevelModes and level_key in VentLevels:
            level_code = VentLevels[level_key]
            self.ventLevel = level_key
        elif self.mode in HumLevelModes and level_key in HumLevels:
            level_code = HumLevels[level_key]
            self.humLevel = level_key
        else:
            logging.warning(f"Level {level_key} ({type(level_key)}) is unknown for mode {self.mode}")
            return

        self.mqtt_send_client.publish(self.config["MQTT"]["ir_topic"],
                                 f"0x{level_code[0:4]} 0x{level_code[4:8]} 2")
        self.mqtt_send_client.loop()
        self.mqtt_send_client.publish(f"{self.config["MQTT"]["topic"]}/level",
                                         level_key,
                                         retain=True)
        self.mqtt_send_client.loop()

        logging.info(f"Set level to {level_key}")

    def set_mode(self, mode_key):
        mode_code = Modes[mode_key]

        self.mqtt_send_client.publish(self.config["MQTT"]["ir_topic"],
                                 f"0x{mode_code[0:4]} 0x{mode_code[4:8]} 2")
        self.mqtt_send_client.loop()

        if mode_key not in NonPersistentModes:
            logging.info(f"Set mode to {mode_key}")
            self.mode = mode_key
            self.mqtt_send_client.publish(f"{self.config["MQTT"]["topic"]}/mode",
                                             mode_key,
                                             retain=True)
            self.mqtt_send_client.loop()
        else:
            logging.info(f"Set non-persistent mode to {mode_key}")



    def setup_logging(self, loglevel):
        logging.basicConfig(
            level=loglevel,
            format='%(asctime)s [%(levelname)s] %(message)s',
            # handlers=[
            #     logging.FileHandler(f"/var/log/{self.config["General"]["name"].lower()}.log"),
            #     logging.StreamHandler(sys.stdout)
            # ]
        )

    @staticmethod
    def parse_config(config_path):
        config_parser = configparser.ConfigParser()
        config_parser.read(config_path)

        ini_dict = {}
        for section in config_parser.sections():
            ini_dict[section] = dict(config_parser.items(section))

        return ini_dict

    @staticmethod
    def handle_signal(signum, frame):
        logging.info(f"Received signal {signum}, shutting down.")
        sys.exit(0)

    def run(self):
        logging.info("Daemon started.")
        # Main loop
        try:
            self.message_worker()
        except Exception as e:
            logging.error(f"An error occurred: {e}", exc_info=True)
        finally:
            logging.info("Daemon is exiting.")

    # def __del__(self):
    #     self.mqtt_loop_thread.stop()

def on_message(client, userdata, message):
    logging.info(f"Received message {message.payload.decode()}")


if __name__ == "__main__":
    daemon_name = "Ampyentika"
    if os.geteuid() == 0:
        logging.error("This daemon should not run as root.")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        prog=daemon_name,
        description='Relay daemon providing a high level interface to an Ambientika '
                    'ventilation system to a generic IR sender.')
    parser.add_argument("--config", nargs=1, type=str, default=[f"/etc/{daemon_name.lower()}/{daemon_name.lower()}.ini"])
    parser.add_argument("--loglevel", nargs=1, type=int, default=[logging.INFO])

    args = parser.parse_args()
    config = Path(args.config[0])
    logl = args.loglevel[0]

    daemon = Ampyentika(config_path=config, loglevel=logl)
    daemon.run()
