import unittest
from decimal import Decimal

from services.workflow_rules import (
    daily_interest,
    pro_rata_allocations,
    validate_drawdown,
    weighted_average_spread,
)


class WorkflowRulesTests(unittest.TestCase):
    def test_drawdown_limit_rejects_overdraw(self):
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            validate_drawdown(Decimal('100.01'), Decimal('100.00'))

    def test_drawdown_within_limit_is_allowed(self):
        validate_drawdown(Decimal('100.00'), Decimal('100.00'))

    def test_pro_rata_allocations_sum_exactly_and_put_remainder_on_largest(self):
        allocations = pro_rata_allocations(
            Decimal('100.00'),
            [(1, Decimal('50')), (2, Decimal('30')), (3, Decimal('20'))],
        )
        self.assertEqual(sum((amount for _, amount in allocations), Decimal('0')), Decimal('100.00'))
        self.assertEqual(allocations, [(1, Decimal('50.00')), (2, Decimal('30.00')), (3, Decimal('20.00'))])

    def test_pro_rata_rounding_remainder_goes_to_largest_participant(self):
        allocations = pro_rata_allocations(
            Decimal('0.01'),
            [(1, Decimal('1')), (2, Decimal('1')), (3, Decimal('1'))],
        )
        self.assertEqual(sum((amount for _, amount in allocations), Decimal('0')), Decimal('0.01'))
        self.assertEqual(allocations[0][1], Decimal('0.01'))
        self.assertEqual(allocations[1][1], Decimal('0.00'))

    def test_weighted_average_spread(self):
        result = weighted_average_spread([
            (Decimal('600'), Decimal('2')),
            (Decimal('400'), Decimal('3')),
        ])
        self.assertEqual(result, Decimal('2.4'))

    def test_daily_interest_act_360_uses_percent_rates(self):
        effective, interest = daily_interest(
            Decimal('100000'),
            Decimal('5'),
            Decimal('2'),
            'ACT/360',
        )
        self.assertEqual(effective, Decimal('7'))
        self.assertEqual(interest, Decimal('19.44444444'))

    def test_daily_interest_act_365(self):
        effective, interest = daily_interest(
            Decimal('100000'),
            Decimal('5'),
            Decimal('2'),
            'ACT/365',
        )
        self.assertEqual(effective, Decimal('7'))
        self.assertEqual(interest, Decimal('19.17808219'))


if __name__ == '__main__':
    unittest.main()
