# jmap-calendar-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent see your calendars, find events, and
create or change events on any [JMAP](https://jmap.io) server that supports JMAP for Calendars, such as
[Stalwart](https://stalw.art) 1.0 and later.

One Python file, standard library only.

## Tools

| Tool | What it does |
| --- | --- |
| `list_calendars` | Calendars on the account, and which one is the default |
| `find_calendar_events` | Events in a time range (default: the next 30 days), optionally matching text; recurring events are expanded |
| `create_calendar_event` | New event: title, start, time zone, length or all-day, description, location, attendees |
| `update_calendar_event` | Change only the given fields of an existing event, or move it to another calendar |

Built to be safe to hand to an agent:

- **No delete.** An agent can move or rename an event but cannot remove one.
- **No invitations by default.** Attendees are added without being emailed. Sending invitations needs both
  `JMAP_ALLOW_INVITES=1` on the server and `send_invitations: true` on the call.
- **Use a key with calendar permissions only**, so the agent cannot read or send mail through it.

## Setup

1. Create an API key (app password) for the account. On Stalwart, limit its permissions to the calendar ones.
2. Add the server to your MCP client. For Claude Code:

```bash
claude mcp add calendar \
  -e JMAP_SESSION_URL=https://mail.example.com/.well-known/jmap \
  -e JMAP_BEARER_TOKEN=your-api-key \
  -- uvx jmap-calendar-mcp
```

For several accounts, add one server per account (`calendar-work`, `calendar-home`).

| Variable | Meaning |
| --- | --- |
| `JMAP_SESSION_URL` | The server's JMAP session URL, usually `https://<host>/.well-known/jmap` |
| `JMAP_BEARER_TOKEN` | API key for the account |
| `JMAP_ALLOW_INVITES` | `1` to allow sending invitations when a call asks for it; off by default |

Keep the key out of shell history and config files where you can: load it from your password manager or
secret store when the server starts.

## License

MIT
