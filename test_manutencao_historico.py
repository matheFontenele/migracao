import unittest

import pandas as pd

from movimentos.migracao_manutencao import (
    MAPA_MAINTENANCE_TYPE,
    MigracaoManutencao,
)


COLUNAS_CAPAS = [
    "manutencao_id", "equipamento_id", "tombo", "cliente",
    "orgao_cliente_id", "orgao_id_equip", "situacao_equipamento",
    "usuario", "technician_status", "descricao_manutencao",
    "status_legado", "data_de_entrada", "updated_at", "deleted_at",
]

COLUNAS_ITENS = [
    "item_manutencao_id", "manutencao_id", "usuario",
    "descricao_item_manutencao", "technician_status",
    "maintenance_types", "created_at", "updated_at", "deleted_at",
]


def _capa(**kwargs):
    base = {
        "manutencao_id": 1,
        "equipamento_id": 100,
        "tombo": "100",
        "cliente": None,
        "orgao_cliente_id": None,
        "orgao_id_equip": 1,
        "situacao_equipamento": 1,
        "usuario": 2,
        "technician_status": 2,
        "descricao_manutencao": "Revisão",
        "status_legado": 1,
        "data_de_entrada": pd.Timestamp("2026-11-01 10:00:00"),
        "updated_at": pd.Timestamp("2026-11-01 10:00:00"),
        "deleted_at": None,
    }
    base.update(kwargs)
    return base


def _item(**kwargs):
    base = {
        "item_manutencao_id": 1,
        "manutencao_id": 1,
        "usuario": 2,
        "descricao_item_manutencao": None,
        "technician_status": 2,
        "maintenance_types": 20,
        "created_at": pd.Timestamp("2026-11-01 10:00:00"),
        "updated_at": pd.Timestamp("2026-11-01 10:00:00"),
        "deleted_at": None,
    }
    base.update(kwargs)
    return base


def _instancia(usuarios=(1, 2, 3), equip_por_numero=None):
    obj = MigracaoManutencao.__new__(MigracaoManutencao)
    obj.usuarios_validos = set(usuarios)
    obj.dados = {"dict_equip_ref_por_number": equip_por_numero or {}}
    obj.now = "2026-11-23 00:00:00"
    return obj


class MapaTipoManutencaoTests(unittest.TestCase):
    def test_mapa_cobre_servicos_legados(self):
        esperado = {
            1: 8,   # REVISÃO_MANUTENÇÃO_OTRS
            2: 8,   # MANUTENÇÃO_REVISÃO_OTRS
            3: 7,   # UPGRADE -> UPGRADE/DOWNGRADE
            4: 1,   # MONTAGEM
            5: 6,   # OTRS
            16: 5,  # MANUTENÇÃO EXTERNA
            17: 2,  # BENCHMARK
            18: 3,  # VISTORIA
            19: 4,  # DESMONTAGEM
            20: 5,  # MANUTENÇÃO
            21: 6,  # OTRS
        }
        self.assertEqual(MAPA_MAINTENANCE_TYPE, esperado)


class MontarBatchesTests(unittest.TestCase):
    def _montar(self, capas, itens, equipamentos, **kwargs):
        obj = _instancia(**kwargs)
        return obj._montar_batches(
            pd.DataFrame(capas, columns=COLUNAS_CAPAS),
            pd.DataFrame(itens, columns=COLUNAS_ITENS),
            set(equipamentos),
        )

    def test_historico_completo_abertas_e_fechadas(self):
        capas = [
            _capa(manutencao_id=1, status_legado=1),
            _capa(manutencao_id=2, status_legado=0),
        ]
        itens = [
            _item(item_manutencao_id=1, manutencao_id=1),
            _item(item_manutencao_id=2, manutencao_id=2),
        ]
        batch, itens_batch, _, sem_equip, sem_manut = self._montar(
            capas, itens, {100}
        )
        self.assertEqual(sem_equip, 0)
        self.assertEqual(sem_manut, 0)
        self.assertEqual(len(batch), 2)
        self.assertEqual(len(itens_batch), 2)
        self.assertEqual(batch[0]["is_closed"], 0)
        self.assertEqual(batch[1]["is_closed"], 1)

    def test_equipamento_por_id_mesmo_com_tombo_ambiguo(self):
        capas = [_capa(equipamento_id=555, tombo="999")]
        batch, _, _, sem_equip, _ = self._montar(
            capas, [], {555}, equip_por_numero={"999": 777}
        )
        self.assertEqual(sem_equip, 0)
        self.assertEqual(batch[0]["equipment_id"], 555)

    def test_fallback_por_tombo_quando_id_nao_existe(self):
        capas = [_capa(equipamento_id=555, tombo="999")]
        batch, _, _, sem_equip, _ = self._montar(
            capas, [], {777}, equip_por_numero={"999": 777}
        )
        self.assertEqual(sem_equip, 0)
        self.assertEqual(batch[0]["equipment_id"], 777)

    def test_manutencao_e_item_sem_equipamento_sao_pulados(self):
        capas = [_capa(equipamento_id=555, tombo="999")]
        itens = [_item(manutencao_id=1)]
        batch, itens_batch, _, sem_equip, sem_manut = self._montar(
            capas, itens, set()
        )
        self.assertEqual(batch, [])
        self.assertEqual(itens_batch, [])
        self.assertEqual(sem_equip, 1)
        self.assertEqual(sem_manut, 1)

    def test_status_equipamento_atualizado_so_para_em_manutencao(self):
        capas = [
            _capa(manutencao_id=1, equipamento_id=100, situacao_equipamento=3),
            _capa(manutencao_id=2, equipamento_id=101, situacao_equipamento=1),
            _capa(manutencao_id=3, equipamento_id=102, situacao_equipamento=7),
        ]
        _, _, equipamentos, _, _ = self._montar(capas, [], {100, 101, 102})
        self.assertEqual(equipamentos, {100})

    def test_tecnico_do_item_tem_precedencia(self):
        capas = [_capa(usuario=2)]
        itens = [
            _item(item_manutencao_id=1, usuario=3),
            _item(item_manutencao_id=2, usuario=999),  # inválido -> capa
            _item(item_manutencao_id=3, usuario=None),  # vazio -> capa
        ]
        _, itens_batch, _, _, _ = self._montar(capas, itens, {100})
        self.assertEqual([i["technician_id"] for i in itens_batch], [3, 2, 2])

    def test_capa_fechada_sempre_concluida(self):
        capas = [_capa(manutencao_id=1, status_legado=0)]
        itens = [
            _item(item_manutencao_id=1, manutencao_id=1, technician_status=4),
            _item(item_manutencao_id=2, manutencao_id=1, technician_status=2),
        ]
        _, itens_batch, _, _, _ = self._montar(capas, itens, {100})
        self.assertEqual([i["maintenance_status_id"] for i in itens_batch], [2, 2])

    def test_capa_aberta_mantem_regra_por_situacao(self):
        capas = [_capa(manutencao_id=1, status_legado=1)]
        itens = [
            _item(item_manutencao_id=1, manutencao_id=1, technician_status=2),
            _item(item_manutencao_id=2, manutencao_id=1, technician_status=4),
        ]
        _, itens_batch, _, _, _ = self._montar(capas, itens, {100})
        self.assertEqual([i["maintenance_status_id"] for i in itens_batch], [2, 1])

    def test_servico_desconhecido_cai_em_manutencao(self):
        capas = [_capa()]
        itens = [
            _item(item_manutencao_id=1, maintenance_types=None),
            _item(item_manutencao_id=2, maintenance_types=999),
            _item(item_manutencao_id=3, maintenance_types=18),
        ]
        _, itens_batch, _, _, _ = self._montar(capas, itens, {100})
        self.assertEqual(
            [i["maintenance_type_id"] for i in itens_batch], [5, 5, 3]
        )

    def test_data_da_manutencao_e_date(self):
        capas = [_capa(data_de_entrada=pd.Timestamp("2024-12-31 23:59:59"))]
        batch, _, _, _, _ = self._montar(capas, [], {100})
        self.assertEqual(str(batch[0]["maintenance_date"]), "2024-12-31")

    def test_item_sem_observacao_herda_motivo_da_capa(self):
        capas = [_capa(descricao_manutencao="Placa queimada")]
        itens = [
            _item(item_manutencao_id=1, descricao_item_manutencao=None),
            _item(item_manutencao_id=2, descricao_item_manutencao="Troca de SSD"),
        ]
        _, itens_batch, _, _, _ = self._montar(capas, itens, {100})
        self.assertEqual(itens_batch[0]["details"], "Placa queimada")
        self.assertEqual(itens_batch[1]["details"], "Troca de SSD")


if __name__ == "__main__":
    unittest.main()
