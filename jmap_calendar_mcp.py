#!/usr/bin/env python3
"""MCP server (stdio) for JMAP Calendars: list calendars, find events, create, update and (if allowed) delete events.

Talks to any JMAP server with urn:ietf:params:jmap:calendars (Stalwart 1.0+). Standard library only.

  JMAP_SESSION_URL   e.g. https://mail.example.com/.well-known/jmap
  JMAP_BEARER_TOKEN  an API key; give it calendar permissions only
  JMAP_ALLOW_INVITES set to 1 to let create/update send invitations when asked (off by default)
  JMAP_ALLOW_DELETE  set to 1 to offer delete_calendar_event (off by default: an agent can then move or rename
                     an event but not remove it)
"""
import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

CAL = "urn:ietf:params:jmap:calendars"
USING = ["urn:ietf:params:jmap:core", CAL]
EVENT_PROPS = ["id", "calendarIds", "title", "description", "start", "timeZone", "duration", "showWithoutTime",
               "locations", "participants", "recurrenceRules", "status", "updated"]


class Jmap:
    def __init__(self, session_url, token):
        self.token = token
        s = self._http("GET", session_url)
        self.api_url = s["apiUrl"]
        self.account_id = s["primaryAccounts"].get(CAL)
        if not self.account_id:
            raise SystemExit("this key has no calendar access: give it the calendar permissions on the server")
        self.username = s["username"]

    def _http(self, method, url, body=None):
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def call(self, method, args):
        res = self._http("POST", self.api_url, {
            "using": USING, "methodCalls": [[method, {"accountId": self.account_id, **args}, "0"]]})
        name, out, _ = res["methodResponses"][0]
        if name == "error":
            raise RuntimeError(f"{method}: {out.get('type')}: {out.get('description', '')}")
        return out


def _duration(minutes):
    h, m = divmod(int(minutes), 60)
    return "PT" + (f"{h}H" if h else "") + (f"{m}M" if m or not h else "")


def _local(dt):
    """Accept 2026-11-03T10:00, 2026-11-03 10:00 or 2026-11-03T10:00:00 and return JSCalendar LocalDateTime."""
    return datetime.fromisoformat(dt.replace(" ", "T")).replace(tzinfo=None).isoformat(timespec="seconds")


def _utc(d):
    t = datetime.fromisoformat(d.replace(" ", "T"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Calendar:
    def __init__(self, jm):
        self.jm = jm
        self.allow_invites = os.environ.get("JMAP_ALLOW_INVITES") == "1"
        self.allow_delete = os.environ.get("JMAP_ALLOW_DELETE") == "1"

    def calendars(self):
        cals = self.jm.call("Calendar/get", {"properties": ["id", "name", "isDefault", "myRights"]})["list"]
        return [{"id": c["id"], "name": c["name"], "default": bool(c.get("isDefault")),
                 "writable": bool((c.get("myRights") or {}).get("mayWriteAll"))} for c in cals]

    def _calendar_id(self, name_or_id):
        cals = self.calendars()
        if not name_or_id:
            for c in cals:
                if c["default"]:
                    return c["id"]
            return cals[0]["id"]
        for c in cals:
            if name_or_id in (c["id"], c["name"]) or c["name"].lower() == name_or_id.lower():
                return c["id"]
        raise ValueError(f"no calendar {name_or_id!r}; have {[c['name'] for c in cals]}")

    def find(self, after=None, before=None, text=None, calendar=None, limit=50):
        now = datetime.now(timezone.utc)
        f = {"after": _utc(after) if after else now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "before": _utc(before) if before else (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if text:
            f["text"] = text
        if calendar:
            f["inCalendar"] = self._calendar_id(calendar)
        ids = self.jm.call("CalendarEvent/query", {"filter": f, "sort": [{"property": "start"}],
                                                    "expandRecurrences": True, "limit": int(limit)})["ids"]
        if not ids:
            return []
        return self.jm.call("CalendarEvent/get", {"ids": ids, "properties": EVENT_PROPS})["list"]

    def _event_fields(self, a, create):
        e = {}
        if a.get("title"):
            e["title"] = a["title"]
        if a.get("all_day"):
            e["showWithoutTime"] = True
            if a.get("start"):
                e["start"] = _local(a["start"][:10] + "T00:00:00")
            e["duration"] = f"P{int(a.get('days') or 1)}D"
        else:
            if a.get("start"):
                e["start"] = _local(a["start"])
            if a.get("time_zone"):
                e["timeZone"] = a["time_zone"]
            if a.get("duration_minutes") or create:
                e["duration"] = _duration(a.get("duration_minutes") or 60)
        if "description" in a:
            e["description"] = a["description"]
        if a.get("location"):
            e["locations"] = {"1": {"@type": "Location", "name": a["location"]}}
        if a.get("attendees"):
            e["participants"] = {f"p{i}": {"@type": "Participant", "email": addr,
                                           "sendTo": {"imip": f"mailto:{addr}"}, "roles": {"attendee": True}}
                                 for i, addr in enumerate(a["attendees"], 1)}
        return e

    def _send(self, a):
        if not a.get("send_invitations"):
            return False
        if not self.allow_invites:
            raise PermissionError("sending invitations is turned off for this connector (JMAP_ALLOW_INVITES)")
        return True

    def create(self, a):
        if not a.get("title") or not a.get("start"):
            raise ValueError("title and start are required")
        if not a.get("all_day") and not a.get("time_zone"):
            raise ValueError("time_zone is required for a timed event, e.g. America/Chicago")
        e = {"@type": "Event", "uid": str(uuid.uuid4()), "calendarIds": {self._calendar_id(a.get("calendar")): True},
             **self._event_fields(a, True)}
        out = self.jm.call("CalendarEvent/set", {"create": {"new": e}, "sendSchedulingMessages": self._send(a)})
        if "new" not in (out.get("created") or {}):
            raise RuntimeError(f"not created: {(out.get('notCreated') or {}).get('new')}")
        return self.jm.call("CalendarEvent/get", {"ids": [out["created"]["new"]["id"]],
                                                  "properties": EVENT_PROPS})["list"][0]

    def update(self, a):
        eid = a.get("event_id")
        if not eid:
            raise ValueError("event_id is required (find it with find_calendar_events)")
        patch = self._event_fields(a, False)
        if a.get("calendar"):
            patch["calendarIds"] = {self._calendar_id(a["calendar"]): True}
        if not patch:
            raise ValueError("nothing to change")
        out = self.jm.call("CalendarEvent/set", {"update": {eid: patch}, "sendSchedulingMessages": self._send(a)})
        if eid not in (out.get("updated") or {}):
            raise RuntimeError(f"not updated: {(out.get('notUpdated') or {}).get(eid)}")
        return self.jm.call("CalendarEvent/get", {"ids": [eid], "properties": EVENT_PROPS})["list"][0]

    def delete(self, a):
        """Removes the whole event, every occurrence of a recurring one. No cancellations are sent."""
        if not self.allow_delete:
            raise PermissionError("deleting events is turned off for this connector (JMAP_ALLOW_DELETE)")
        eid = a.get("event_id")
        if not eid:
            raise ValueError("event_id is required (find it with find_calendar_events)")
        found = self.jm.call("CalendarEvent/get", {"ids": [eid], "properties": ["id", "title", "start", "timeZone"]})
        if not found["list"]:
            raise RuntimeError(f"no event {eid}")
        out = self.jm.call("CalendarEvent/set", {"destroy": [eid], "sendSchedulingMessages": False})
        if eid not in (out.get("destroyed") or []):
            raise RuntimeError(f"not deleted: {(out.get('notDestroyed') or {}).get(eid)}")
        return {"deleted": found["list"][0]}


EVENT_INPUT = {
    "title": {"type": "string"},
    "start": {"type": "string", "description": "Local start time, YYYY-MM-DDTHH:MM (a date alone for all-day)"},
    "time_zone": {"type": "string", "description": "IANA zone for a timed event, e.g. America/Chicago"},
    "duration_minutes": {"type": "integer", "minimum": 1, "description": "Default 60"},
    "all_day": {"type": "boolean"},
    "days": {"type": "integer", "minimum": 1, "description": "Length of an all-day event, default 1"},
    "description": {"type": "string"},
    "location": {"type": "string"},
    "calendar": {"type": "string", "description": "Calendar name or id; default calendar if omitted"},
    "attendees": {"type": "array", "items": {"type": "string"}, "description": "Email addresses"},
    "send_invitations": {"type": "boolean", "default": False,
                         "description": "Email invitations to attendees. Only when the user asked for it."},
}

TOOLS = [
    {"name": "list_calendars", "description": "List the calendars this account can see, with which is the default.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "find_calendar_events",
     "description": "Find events between two times (default: the next 30 days), optionally matching text. "
                    "Recurring events are expanded into their occurrences.",
     "inputSchema": {"type": "object", "properties": {
         "after": {"type": "string", "description": "ISO date/time; UTC unless an offset is given"},
         "before": {"type": "string", "description": "ISO date/time; UTC unless an offset is given"},
         "text": {"type": "string"}, "calendar": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}}},
    {"name": "create_calendar_event",
     "description": "Create an event. Confirm title, time and time zone with the user first. Attendees are added "
                    "without being emailed unless send_invitations is true.",
     "inputSchema": {"type": "object", "properties": EVENT_INPUT, "required": ["title", "start"]}},
    {"name": "update_calendar_event",
     "description": "Change an existing event: give event_id and only the fields to change.",
     "inputSchema": {"type": "object", "properties": {"event_id": {"type": "string"}, **EVENT_INPUT},
                     "required": ["event_id"]}},
]

DELETE_TOOL = {
    "name": "delete_calendar_event",
    "description": "Delete an event, every occurrence if it repeats. Only when the user asked to remove this event: "
                   "find it first and confirm which one. Attendees are not notified.",
    "inputSchema": {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]}}


def tools():
    return TOOLS + ([DELETE_TOOL] if os.environ.get("JMAP_ALLOW_DELETE") == "1" else [])


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    cal = None
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        method, id_ = req.get("method"), req.get("id")
        if id_ is None:
            continue
        if method == "initialize":
            reply(id_, {"protocolVersion": req["params"].get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "jmap-calendar", "version": "0.2.0"}})
        elif method == "tools/list":
            reply(id_, {"tools": tools()})
        elif method == "tools/call":
            p = req["params"]
            a = p.get("arguments") or {}
            try:
                cal = cal or Calendar(Jmap(os.environ["JMAP_SESSION_URL"], os.environ["JMAP_BEARER_TOKEN"]))
                out = {"list_calendars": lambda: cal.calendars(),
                       "find_calendar_events": lambda: cal.find(a.get("after"), a.get("before"), a.get("text"),
                                                                a.get("calendar"), a.get("limit") or 50),
                       "create_calendar_event": lambda: cal.create(a),
                       "update_calendar_event": lambda: cal.update(a),
                       "delete_calendar_event": lambda: cal.delete(a)}[p["name"]]()
                reply(id_, {"content": [{"type": "text", "text": json.dumps(out, indent=1)}]})
            except Exception as e:
                reply(id_, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif method == "ping":
            reply(id_, {})
        else:
            reply(id_, error={"code": -32601, "message": f"no method {method}"})


if __name__ == "__main__":
    main()
