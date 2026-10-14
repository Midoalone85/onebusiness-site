# ONE COMMAND v1.8

## New
- With an active PC session and connected Windows Agent, the owner can open the exact ONE ERP and ONE COMMAND web addresses in the Windows default browser from the private console, including when accessing the console from a phone browser.
- New controls use the same single-use, two-minute, owner-session-bound preview approval and last-moment agent authorization as the existing Notepad/Calculator actions.
- No user-supplied URL, file path, shell command or general browser navigation is accepted.
- Action failures are separately displayed as failures, not successful executions.

## Verified
Local API regression tests covered both exact-site actions, one-time approvals, replay attempts, arbitrary URL rejection, unsafe appended commands, agent authorization and failed launch reporting. Headless Chrome test verified new buttons, owner confirmation, queue and stop, with no uncaught JavaScript errors.

## Important limitations
Launching the approved URL is not screen control or autonomous ChatGPT remote operation. The tests did not open an actual browser window on the user's production PC. ONE COMMAND continues to store sessions and logs in volatile Render memory, and mobile integration is still browser-only. Full remote screen/mouse control or broader agent access must wait until the owner access key (previously exposed in chat) has been rotated and stronger authentication is enabled. No production ONE ERP records were changed.
