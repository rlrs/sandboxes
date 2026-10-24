"""Node-local model waits: which relays take the local path (runtime/noded runs the waits)."""
import unittest

from sandboxes.local_wait import relay_endpoints
from sandboxes.relay_network import NetworkRelay

TEST_TIER = "contract"


class RelayFlowTests(unittest.TestCase):
    def test_only_private_literal_relays_take_the_local_path(self):
        relays = {"default": NetworkRelay.parse("default", "10.42.0.2:8092"),
                  "ingress": NetworkRelay("ingress", "relay.example.org", 443),
                  "public": NetworkRelay("public", "8.8.4.4", 443)}
        self.assertEqual(relay_endpoints(relays), (("10.42.0.2", 8092),))

    def test_a_relay_named_for_private_addresses_takes_the_local_path(self):
        relays = {"default": NetworkRelay.parse("default", "sandbox-gateway-gateway-1:8092"),
                  "ingress": NetworkRelay("ingress", "relay.example.org", 443),
                  "gone": NetworkRelay("gone", "gone.internal", 8092)}
        answers = {"default": ["10.36.102.75"], "ingress": ["10.36.0.9", "8.8.4.4"]}

        def resolve(name):
            if name not in answers:
                raise OSError("no such name")
            return answers[name]

        # A name answered only by private addresses is matched at those
        # addresses; one with any public answer is a TLS ingress.
        self.assertEqual(relay_endpoints(relays, resolve), (("10.36.102.75", 8092),))
        self.assertEqual(relay_endpoints(relays), ())


if __name__ == "__main__":
    unittest.main()
