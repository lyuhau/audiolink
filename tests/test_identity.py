"""services/identity.py: the Pi's key, name and list."""
import os
import shutil
import tempfile
import unittest

from audiolink.crypto import ed25519_public
from audiolink.paths import load_state, save_state
from audiolink.services.access import OWNER, USER, Refused
from audiolink.services.identity import IdentityService


def key():
    return ed25519_public(os.urandom(32))


class List(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.id = IdentityService(self.dir)
        self.desktop, self.laptop = key(), key()

    def test_new_pi_is_unclaimed(self):
        self.assertFalse(self.id.claimed())
        self.assertEqual(self.id.members(), [])
        self.assertEqual(self.id.info()["owners"], 0)
        self.assertEqual(set(load_state(self.id.path)), {"seed", "name", "members"})

    def test_claim_once(self):
        members = self.id.claim(self.desktop.hex(), "desktop.local")
        self.assertEqual([(m["name"], m["role"]) for m in members], [("desktop.local", OWNER)])
        self.assertTrue(self.id.claimed())
        with self.assertRaises(Refused) as e:
            self.id.claim(self.laptop, "laptop")
        self.assertEqual(e.exception.code, "claimed")
        self.assertEqual(IdentityService(self.dir).members(), members)       # kept

    def test_the_last_owner_stays(self):
        self.id.claim(self.desktop, "desktop")
        with self.assertRaises(Refused) as e:
            self.id.remove(self.desktop.hex())
        self.assertEqual(e.exception.code, "last_owner")
        self.id.add(self.laptop, "laptop", OWNER)
        self.id.remove(self.desktop.hex())
        self.assertEqual(self.id.role_of(self.desktop), None)
        self.assertEqual(self.id.role_of(self.laptop), OWNER)

    def test_users_remove_only_themselves(self):
        self.id.claim(self.desktop, "desktop")
        self.id.add(self.laptop, "laptop", USER)
        mom = key()
        self.id.add(mom, "mom", USER)
        with self.assertRaises(Refused) as e:
            self.id.remove_for(self.laptop, USER, {"name": "mom"})
        self.assertEqual(e.exception.code, "not_owner")
        self.assertEqual(self.id.remove_for(self.laptop, USER, {"key": self.laptop.hex()}), {"ok": True})
        self.assertEqual(self.id.remove_for(self.desktop, OWNER, {"name": "MOM"}), {"ok": True})
        with self.assertRaises(Refused) as e:
            self.id.remove_for(self.desktop, OWNER, {"name": "mom"})
        self.assertEqual(e.exception.code, "not_found")

    def test_names_stay_unique_and_roles_only_go_up(self):
        self.id.claim(self.desktop, "desktop")
        self.assertEqual(self.id.add(self.laptop, "desktop", USER)["name"], "desktop-2")
        self.assertEqual(self.id.add(self.laptop, "laptop", OWNER)["role"], OWNER)
        self.assertEqual(self.id.add(self.laptop, "laptop", USER)["role"], OWNER)
        self.assertEqual(len(self.id.members()), 2)

    def test_start_over(self):
        self.id.claim(self.desktop, "desktop")
        pub = self.id.public()
        changed = []
        self.id.subscribe(lambda: changed.append(1))
        self.id.start_over()
        self.assertNotEqual(self.id.public(), pub)
        self.assertFalse(self.id.claimed())
        self.assertEqual(changed, [1])

    def test_identity_deleted_from_the_card_starts_over_like_the_pins(self):   # README: Lost the owner computer
        import glob
        self.id.claim(self.desktop, "desktop")
        self.id.rename("kitchen")
        pub, name = self.id.public(), self.id.data["name"]
        self.id.start_over()
        pins = (self.id.data["name"], self.id.members())
        self.id.claim(self.desktop, "desktop")
        self.id.rename("kitchen")
        for f in glob.glob(str(self.id.path) + "*"):        # identity.json.a, .b (identity.json: older)
            os.remove(f)
        from unittest import mock
        with mock.patch("audiolink.services.identity.board_serial", lambda: self.id.serial):
            again = IdentityService(self.dir)               # the next boot, on the same board
        self.assertNotIn(again.public(), (pub, self.id.public()))
        self.assertEqual((again.data["name"], again.members()), pins)
        self.assertNotEqual(name, again.data["name"])
        self.assertFalse(again.claimed())

    def test_rename(self):
        self.id.handle("rename", {"name": "Kitchen"})
        self.assertEqual(self.id.pi_name(), "kitchen")
        with self.assertRaises(ValueError):
            self.id.rename("no spaces")

    def test_invites_go_to_the_inviter(self):
        asked = []
        self.id.inviter = lambda name, role: asked.append((name, role)) or {"code": "123456", "expires_in": 300}
        self.assertEqual(self.id.handle("invite", {"name": "mom"})["code"], "123456")
        self.id.handle("invite", {"name": "laptop", "role": "owner"})
        self.assertEqual(asked, [("mom", USER), ("laptop", OWNER)])
        with self.assertRaises(ValueError):
            self.id.handle("invite", {"name": "x", "role": "admin"})


class FromBefore(unittest.TestCase):
    """identity.json from before the list: key and name kept, unclaimed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.seed = os.urandom(32).hex()
        self.old = {"seed": self.seed, "secret": os.urandom(16).hex(), "name": "green-rowan", "claimed": True,
                    "discoverable": True}

    def test_load(self):
        save_state(os.path.join(self.dir, "identity.json"), self.old)
        ident = IdentityService(self.dir)
        self.assertEqual((ident.data["seed"], ident.pi_name(), ident.claimed()), (self.seed, "green-rowan", False))
        self.assertEqual(set(load_state(ident.path)), {"seed", "name", "members"})

    def test_damaged_entries_are_dropped(self):
        save_state(os.path.join(self.dir, "identity.json"), {
            "seed": self.seed, "name": "green-rowan",
            "members": [{"key": "zz", "name": "x", "role": OWNER}, {"key": key().hex(), "name": "ok", "role": USER},
                        {"key": key().hex(), "name": "y", "role": "admin"}]})
        self.assertEqual([m["name"] for m in IdentityService(self.dir).members()], ["ok"])


if __name__ == "__main__":
    unittest.main()
