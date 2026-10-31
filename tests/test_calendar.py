import pytest

import jmap_calendar_mcp as m


class FakeJmap:
    """Records CalendarEvent/set calls and answers like a JMAP server with two calendars."""

    def __init__(self):
        self.sets = []
        self.events = {}

    def call(self, method, args):
        if method == "Calendar/get":
            return {"list": [{"id": "c1", "name": "Work", "isDefault": True, "myRights": {"mayWriteAll": True}},
                             {"id": "c2", "name": "Home", "myRights": {"mayWriteAll": True}}]}
        if method == "CalendarEvent/set":
            self.sets.append(args)
            if "destroy" in args:
                gone = [i for i in args["destroy"] if self.events.pop(i, None)]
                return {"destroyed": gone}
            if "create" in args:
                self.events["e1"] = {"id": "e1", **args["create"]["new"]}
                return {"created": {"new": {"id": "e1"}}}
            eid = next(iter(args["update"]))
            if eid not in self.events:
                return {"notUpdated": {eid: {"type": "notFound"}}}
            self.events[eid].update(args["update"][eid])
            return {"updated": {eid: None}}
        if method == "CalendarEvent/get":
            return {"list": [self.events[i] for i in args["ids"] if i in self.events]}
        if method == "CalendarEvent/query":
            self.query = args
            return {"ids": []}
        raise AssertionError(method)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.delenv("JMAP_ALLOW_INVITES", raising=False)
    monkeypatch.delenv("JMAP_ALLOW_DELETE", raising=False)
    return m.Calendar(FakeJmap())


def test_create_timed_event(cal):
    ev = cal.create({"title": "Plumber", "start": "2026-11-03 10:00", "time_zone": "America/Chicago",
                     "duration_minutes": 90, "location": "Unit 4"})
    sent = cal.jm.sets[0]
    assert sent["sendSchedulingMessages"] is False
    assert ev["calendarIds"] == {"c1": True}  # default calendar
    assert (ev["start"], ev["timeZone"], ev["duration"]) == ("2026-11-03T10:00:00", "America/Chicago", "PT1H30M")
    assert ev["locations"]["1"]["name"] == "Unit 4"


def test_create_all_day_in_named_calendar(cal):
    ev = cal.create({"title": "Move-in", "start": "2026-11-05", "all_day": True, "days": 2, "calendar": "home"})
    assert ev["calendarIds"] == {"c2": True}
    assert (ev["showWithoutTime"], ev["start"], ev["duration"]) == (True, "2026-11-05T00:00:00", "P2D")


def test_timed_event_needs_zone(cal):
    with pytest.raises(ValueError, match="time_zone"):
        cal.create({"title": "x", "start": "2026-11-03T10:00"})


def test_invitations_blocked_unless_enabled(cal, monkeypatch):
    args = {"title": "x", "start": "2026-11-03T10:00", "time_zone": "UTC", "attendees": ["a@b.c"],
            "send_invitations": True}
    with pytest.raises(PermissionError):
        cal.create(args)
    monkeypatch.setenv("JMAP_ALLOW_INVITES", "1")
    cal = m.Calendar(cal.jm)
    cal.create(args)
    assert cal.jm.sets[-1]["sendSchedulingMessages"] is True


def test_attendees_added_without_invites(cal):
    ev = cal.create({"title": "x", "start": "2026-11-03T10:00", "time_zone": "UTC", "attendees": ["a@b.c"]})
    assert ev["participants"]["p1"]["email"] == "a@b.c"
    assert cal.jm.sets[0]["sendSchedulingMessages"] is False


def test_update_patches_only_given_fields(cal):
    cal.create({"title": "x", "start": "2026-11-03T10:00", "time_zone": "UTC"})
    cal.update({"event_id": "e1", "start": "2026-11-04T09:30"})
    assert cal.jm.sets[-1]["update"] == {"e1": {"start": "2026-11-04T09:30:00"}}
    with pytest.raises(RuntimeError):
        cal.update({"event_id": "missing", "title": "y"})
    with pytest.raises(ValueError):
        cal.update({"event_id": "e1"})


def test_find_defaults_to_next_30_days(cal):
    assert cal.find(text="dentist") == []
    f = cal.jm.query["filter"]
    assert f["text"] == "dentist" and f["after"].endswith("Z") and f["before"] > f["after"]


def test_delete_off_by_default(cal):
    assert all("delete" not in t["name"] for t in m.tools())
    cal.create({"title": "x", "start": "2026-11-03T10:00", "time_zone": "UTC"})
    with pytest.raises(PermissionError):
        cal.delete({"event_id": "e1"})
    assert "e1" in cal.jm.events


def test_delete_when_allowed(cal, monkeypatch):
    monkeypatch.setenv("JMAP_ALLOW_DELETE", "1")
    assert any(t["name"] == "delete_calendar_event" for t in m.tools())
    cal = m.Calendar(cal.jm)
    cal.create({"title": "Dentist", "start": "2026-11-03T10:00", "time_zone": "UTC"})
    assert cal.delete({"event_id": "e1"})["deleted"]["title"] == "Dentist"
    assert cal.jm.sets[-1] == {"destroy": ["e1"], "sendSchedulingMessages": False}
    with pytest.raises(RuntimeError, match="no event"):
        cal.delete({"event_id": "e1"})
