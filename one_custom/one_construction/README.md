# ONE Construction: safe first implementation

Development branch only. Not deployed or approved for commercial use.

## Business design

- **Construction project** is the cost-center anchor. One project may contain multiple service work items and multiple progress certificates.
- **Finishing, painting, electrical and plumbing** are standard service products, not separate modules.
- Each bill-of-quantities work item stores a service, work phase, UoM, contracted quantity, and unit rate.
- Progress certificates record executed quantities and enforce a no-over-certification check at approval.
- Approval requires **ONE Construction Manager** or system administrator rights.
- Only approved certificates with **zero retention and zero advance recovery** can generate an **unposted draft customer invoice**.
- If a project has an analytic account, generated invoice lines allocate 100% to that cost center.
- Dashboard financial measures show contract value, approved value, remaining value, and preliminary analytic costs. Contribution to date is **not** final profit.
- Multi-company access rules restrict records to allowed companies.
- Company settings expose default retention %, but fiscal accounting of retention/advance is intentionally blocked until configured and tested.

## How to try on disposable staging

1. Back up and verify a test database. Do **not** install on production.
2. Make sure Odoo 20, invoicing and the standard service catalog are installed.
3. Add `one_custom` to Odoo addons path, update apps, install **ONE Construction**.
4. Assign the ONE Construction Manager group to the approver.
5. Create service products (e.g. painting per square meter, electrical per point).
6. Create a project, link an analytic account, enter work items, and start it.
7. Enter a progress certificate; submit, approve, and generate an *unposted* invoice where retention/advance are zero.
8. Validate invoice VAT, journal and analytic allocation in the test database.

## Not yet production-ready

No Odoo runtime/staging tests have been executed for this branch; verify module install + upgrade compatibility first.
No automated retention, advance recovery, subcontractor settlements, stock issue-to-project, or ZATCA clearance workflow has been implemented.
Do not claim regulatory compliance or commercial readiness based on this commit.
Validate data isolation, permissions, financial reconciliation, reversal flows, concurrency, and Saudi tax edge cases before launch.
