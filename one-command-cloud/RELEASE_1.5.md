# ONE COMMAND v1.5

Added: Arabic fixed-command assistant with preview and approval; optional recurring read-only diagnostics every 15 minutes, hour, 6 hours or day.

Start a PC session, type an allowed request and review the proposed action before approval. Commands supported: inspect Windows, inspect Oracle, open Notepad, open Calculator. Other tasks are rejected.

The recurring schedule only runs when a Windows agent polls an awake Render service and the PC session remains active. Server restart, Render free-tier sleep, or agent disconnection may interrupt monitoring. Unlimited sessions are not guaranteed permanent.

This assistant is deterministic and is not a live ChatGPT integration. No arbitrary shell execution, no full Android/iOS control, and no ERP production modification is provided. Owner login expires in 30 minutes. Protect the owner secret and consider MFA and separate durable storage before production authority.

Validated locally: login, approved action preview, scheduled monitoring settings, allowlist restrictions, browser execution, and stop.
