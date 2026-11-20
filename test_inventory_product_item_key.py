import unittest
import ast
from pathlib import Path


def load_key_function():
    source = Path(__file__).parent / "cadastros/equipamentos/migracao_equipamentos.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "_product_item_combination_key")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_product_item_combination_key"]


_product_item_combination_key = load_key_function()


class ProductItemCombinationKeyTest(unittest.TestCase):
    def test_repeated_combination_has_same_key(self):
        first = _product_item_combination_key(1794, 1, 1, 1115)
        repeated = _product_item_combination_key(1794, 1, 1, 1115)

        self.assertEqual(first, repeated)

    def test_distinct_location_organization_or_product_has_distinct_key(self):
        base = _product_item_combination_key(1794, 1, 1, 1115)

        self.assertNotEqual(base, _product_item_combination_key(1794, 1, 2, 1115))
        self.assertNotEqual(base, _product_item_combination_key(1794, 1, 1, 1311))
        self.assertNotEqual(base, _product_item_combination_key(1795, 1, 1, 1115))


if __name__ == "__main__":
    unittest.main()
