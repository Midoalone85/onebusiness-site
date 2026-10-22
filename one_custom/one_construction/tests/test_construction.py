from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestOneConstruction(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "Demo Construction Client"})
        cls.service = cls.env["product.product"].create({
            "name": "Painting per square meter",
            "type": "service",
            "list_price": 100.0,
        })
        cls.project = cls.env["one.construction.project"].create({
            "name": "Test Finishing Project",
            "partner_id": cls.partner.id,
            "boq_line_ids": [(0, 0, {
                "product_id": cls.service.id,
                "name": "Wall painting",
                "uom_id": cls.service.uom_id.id,
                "quantity": 10.0,
                "price_unit": 100.0,
            })],
        })
        cls.project.action_activate()
        cls.work_item = cls.project.boq_line_ids

    def _certificate(self, quantity):
        return self.env["one.construction.certificate"].create({
            "project_id": self.project.id,
            "line_ids": [(0, 0, {
                "boq_line_id": self.work_item.id,
                "quantity": quantity,
            })],
        })

    def test_service_contract_and_approved_progress(self):
        self.assertEqual(self.project.contract_amount, 1000.0)
        cert = self._certificate(4.0)
        self.assertEqual(cert.work_amount, 400.0)
        cert.action_submit()
        cert.action_approve()
        self.assertEqual(self.project.approved_amount, 400.0)
        self.assertEqual(self.work_item.remaining_quantity, 6.0)
        self.assertEqual(self.project.remaining_amount, 600.0)

        with self.assertRaises(UserError):
            cert.line_ids.write({"quantity": 5.0})
        with self.assertRaises(UserError):
            self.work_item.write({"price_unit": 150.0})

    def test_prevent_over_certification(self):
        first = self._certificate(7.0)
        first.action_submit()
        first.action_approve()
        excess = self._certificate(4.0)
        with self.assertRaises(ValidationError):
            excess.action_submit()

    def test_reject_work_from_another_project(self):
        another = self.env["one.construction.project"].create({
            "name": "Unrelated Project",
            "partner_id": self.partner.id,
            "boq_line_ids": [(0, 0, {
                "product_id": self.service.id,
                "name": "Other painting",
                "uom_id": self.service.uom_id.id,
                "quantity": 10.0,
                "price_unit": 100.0,
            })],
        })
        with self.assertRaises(ValidationError):
            self.env["one.construction.certificate"].create({
                "project_id": self.project.id,
                "line_ids": [(0, 0, {
                    "boq_line_id": another.boq_line_ids.id,
                    "quantity": 1.0,
                })],
            })

    def test_approved_certificate_does_not_allow_extra_lines(self):
        cert = self._certificate(3.0)
        cert.action_submit()
        cert.action_approve()
        with self.assertRaises(UserError):
            self.env["one.construction.certificate.line"].create({
                "certificate_id": cert.id,
                "boq_line_id": self.work_item.id,
                "quantity": 2.0,
            })
