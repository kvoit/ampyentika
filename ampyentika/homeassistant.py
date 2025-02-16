import json
import time
from paho.mqtt.client import Client as mqtt_client

from ampyentika.ambientika import HumLevels, Modes


def homeassistant_discover(name: str, unique_id: str, topic: str, client: mqtt_client):
    for field, options in zip(["mode", "level"], [Modes, HumLevels]):
        state_topic = f"{topic}/{field}"
        command_topic = f"{topic}/{field}/set"

        doc = {
            "name": f"{field} ({name})",
            "unique_id": f"{name}_{field}_{unique_id}",
            "state_topic": state_topic,
            "command_topic": command_topic,
            "optimistic": False,
            "retain": True,
            "options": [str(k) for k in options.keys()],
            "device": {
                "name": name,
                "identifiers": [unique_id]
            }
        }
        discovery_msg = json.dumps(doc)
        discovery_topic = f"homeassistant/select/ambientika/{field}_{name}_{unique_id}/config"
        client.publish(discovery_topic, discovery_msg, retain=True)
    return True