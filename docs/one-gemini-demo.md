# ONE Intelligence + Gemini demo integration (staging only)

This optional feature adds a **real Gemini API response** to the ONE Intelligence page,
while retaining ONE ERP's read-only, local financial assistant.

## Boundary: no customer/company records sent

- **Local ONE** button: handles ERP records, permissions and posted-ledger calculations in Odoo.
- **Gemini Demo** button: a system administrator must explicitly confirm each question is
  general and non-confidential. It sends **only the typed question** plus a static,
  public-purpose instruction to Google's Gemini API.
- The feature does **not** look up customers, journal entries, invoices, attachments,
  company records or financial KPIs for Gemini.
- Do not type personal data, national IDs, customer data, invoices, confidential
  contracts or secrets into the Gemini demo. Free-tier content may be used to
  improve Google's products. See Google's official pricing/terms.
- The Gemini key stays server-side and is never embedded in JavaScript or a Git commit.
- Only Odoo system administrators can run the demo; the entire feature is OFF by default.
- This integration is only suitable for sandbox/staging validation until a paid
  data-use agreement or a properly reviewed on-premises model is in place.

## Server configuration (by the administrator)

Create a separate free-tier Google AI Studio API key from:
https://aistudio.google.com/api-keys

On the **staging Odoo server**, use secret-backed environment variables (for example
a root-restricted environment file via systemd or your deployment secret manager).
Do NOT paste the real key in GitHub, chat, browser code or Odoo documents.

Required settings:

| Environment variable | Value |
|---|---|
| `ONE_GEMINI_DEMO_ENABLED` | `1` to enable, otherwise disabled |
| `ONE_GEMINI_API_KEY` | The real Gemini API key stored server-side in a secret |
| `ONE_GEMINI_MODEL` | Optional, defaults to `gemini-3.5-flash-lite` |

Model availability and free quota vary by project and region. The model may be
changed to another accessible Gemini model by an administrator. The host is
hard-coded to Google's official HTTPS API endpoint.

**Free-tier note:** The account/project must actually be on Google's Free Tier;
this code does not verify your billing plan or prevent paid billing if your key
belongs to a billable Google project. Keep usage limits configured in Google AI
Studio and leave the feature disabled on production until reviewed.

## User-facing workflow

1. Open ONE ERP > **ONE Intelligence**.
2. For company-specific balances, use the regular Ask ONE button and built-in ERP reports.
3. For a general question (example: "Explain perpetual versus periodic inventory"),
   enter it in the question field.
4. An administrator checks the opt-in box and clicks **Try Gemini**.
5. ONE shows the external response explicitly labeled **Gemini Demo**.
6. If Gemini is unconfigured, unavailable or quota-limited, use regular Ask ONE.
   There is no silent transfer of business records.

## Verification

Run the offline tests without installing Odoo, setting up a key or hitting the internet:

```sh
python tools/verify_ask_one_finance.py
python tools/verify_one_gemini_demo.py
```

Before deployment, test Odoo module update, Owl/QWeb template compilation, visual
mobile layout, admin/non-admin access, current company record isolation and
that both API-disabled and rate-limit states fail gracefully. Confirm real
Gemini API connectivity in staging using only a nonsensitive prompt.

This feature is **not automatically deployed** by merging or creating its PR.
