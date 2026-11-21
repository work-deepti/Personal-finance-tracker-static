import os
import unittest
from unittest.mock import patch

import app as web

ORIGIN = "https://tracker.example"


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeDatabase:
    """Simulate storage for route tests without contacting Supabase."""
    def __init__(self):
        self.sessions = {}
        self.portfolios = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def execute(self, query, parameters=()):
        if query.startswith("insert into tracker_private.sessions"):
            token, user, _ = parameters
            self.sessions[token] = user
        elif query.startswith("select user_id"):
            user = self.sessions.get(parameters[0])
            return Result((user,) if user else None)
        elif query.startswith("delete from tracker_private.sessions where token_hash"):
            self.sessions.pop(parameters[0], None)
        elif query.startswith("insert into tracker_private.portfolios"):
            self.portfolios.setdefault(parameters[0], {"holdings": [], "history": {}, "chart_cache": {}})
        elif query.startswith("select holdings"):
            data = self.portfolios.get(parameters[0])
            return Result(tuple(data[key] for key in web.fields.values()) if data else None)
        elif query.startswith("update tracker_private.portfolios"):
            field = query.split()[3]
            self.portfolios[parameters[1]][field] = parameters[0].obj


class WebTests(unittest.TestCase):
    def test_private_portfolios_and_signout(self):
        database = FakeDatabase()
        with patch.dict(os.environ, {"APP_ORIGIN": ORIGIN}), patch.object(
            web, "database", return_value=database
        ), patch.object(web, "verify_password", side_effect=lambda email, _: email):
            alice = web.app.test_client()
            bob = web.app.test_client()
            self.assertEqual(alice.get("/api/holdings", base_url=ORIGIN).status_code, 401)
            self.assertEqual(alice.get("/", base_url=ORIGIN).location, "/signin")
            for client, name in ((alice, "alice"), (bob, "bob")):
                response = client.post("/signin", base_url=ORIGIN, headers={"Origin": ORIGIN},
                                       json={"email": name + "@example.com", "password": "test"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("HttpOnly", response.headers["Set-Cookie"])
                self.assertIn("Secure", response.headers["Set-Cookie"])
            record = {"name": "Private investment", "type": "manual", "principal": 100, "value": 150}
            denied = alice.post("/api/holdings", base_url=ORIGIN, headers={"Origin": "https://other.example"},
                                json=record)
            self.assertEqual(denied.status_code, 403)
            created = alice.post("/api/holdings", base_url=ORIGIN, headers={"Origin": ORIGIN}, json=record)
            self.assertEqual(created.status_code, 200)
            self.assertEqual(alice.get("/api/holdings", base_url=ORIGIN).json[0]["name"], record["name"])
            self.assertEqual(bob.get("/api/holdings", base_url=ORIGIN).json, [])
            result = bob.delete("/api/holdings/" + created.json["id"], base_url=ORIGIN,
                                headers={"Origin": ORIGIN}, json={})
            self.assertEqual(result.status_code, 404)
            self.assertEqual(alice.post("/signout", base_url=ORIGIN, headers={"Origin": ORIGIN}).status_code, 200)
            self.assertEqual(alice.get("/api/holdings", base_url=ORIGIN).status_code, 401)
