# ONE COMMAND v1.6

## Security upgrade
Every Arabic command preview now generates a random single-use approval valid for 2 minutes, bound to the exact text and the same signed owner login session. The server rejects changed text, reuse, missing approval, and cross-session approval. Stop and logout invalidate pending previews.

Windows Notepad and Calculator buttons now use the same preview-and-confirm flow. The old direct execution route is disabled.

Before the Windows agent launches either allowed application, it must request an additional one-time authorization from the service. Stop prevents authorizing a pending operation, although it cannot undo an app already launched. Arbitrary shell and ERP modification remain unavailable.

Tests run with nonproduction local credentials: baseline smoke suite, signed approval consistency, other-session misuse, duplicate authorization, final agent permit, stop cancellation and Chrome UI. No actual Windows application or ERP production changes were made as part of these tests.

## Current limits
This is not autonomous ChatGPT remote control. MFA is not yet enrolled; the previous login key appeared in chat and should be changed securely before expanding permissions. Render currently stores command state in memory, so free-tier sleep and restarts clear sessions. The mobile integration is browser-only and cannot control phone applications or system UI. A longer-running, high-trust deployment needs independent durable storage, MFA, and a reviewed computer-use controller.
