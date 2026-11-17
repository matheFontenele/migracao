-- =============================================================================
-- Limpeza do item AVULSO indevido — equipamento 44764 / movimento 9558
-- =============================================================================
-- Objetivo
--   Remover SOMENTE o item do equipamento 44764 (tombo 47613) do movimento
--   AVULSO 9558 (número 120497, COMITÊ ICÓ 2024 / org 1311), criado a partir da
--   linha do Parquet que pertence, na verdade, ao equipamento 45174.
--
--   O movimento 9558 permanece: os itens dos equipamentos 45175 (MONITOR) e
--   45178 (ESTABILIZADOR) são legítimos.
--
-- Premissas (conferidas em 2026-11-14, banco local `controle-interno`)
--   movements        id=9558  number=120497  tipo AVULSO (7)  customer=32
--   service_orders   id=9558  movement_type_id=7
--   service_order_items 40491 (equip 44764) / 40492 (45175) / 40493 (45178)
--   movement_items      40491 (equip 44764) / 40492 (45175) / 40493 (45178)
--   shipments           40491 / 40492 / 40493 (um por item)
--   shipment_movements  40491->9558 / 40492->9558 / 40493->9558
--   shipment_items      40491 / 40492 / 40493 (um por item)
--   volumes             9558 "MIG-9558"  (fica)
--   volume_items        42156->40491 / 42157->40492 / 42158->40493 (só 42156 sai)
--   equipment_history   120673 equip=44764 mi=40491 soi=40491 shipment_item=40491
--                       status=2 reason=SHIPPING_CONFIRMED_SEPARATE (sai)
--   equipment_history   48055 (TRANSACTION_ENTRANCE_EQUIPMENT) NÃO é tocado
--   equipments.id=44764 last_movement_item_customer_id = 40491 (volta a NULL)
--
--   Nenhum registro em: return_confirmations_items, romaneios,
--   shipment_documents, shipment_document_movements, maintenance_items,
--   service_order_accessories, service_order_item_extra_equipments,
--   document_audit_logs.  (conferido por id e por shipment_id)
--
-- Chaves fixas de propósito: nenhum DELETE usa predicado amplo.
--
-- ORDEM OBRIGATÓRIA (há FKs declaradas):
--   equipment_history -> volume_items -> shipment_items -> shipment_movements
--   -> shipments -> movement_items -> service_order_items -> UPDATE equipments
--   (movement_items referencia service_order_items, por isso o item do pedido
--    só sai depois do item do movimento; shipments só sai depois de
--    shipment_items e shipment_movements.)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1) PRÉVIA (rodar antes; resultado esperado anotado em cada consulta)
-- -----------------------------------------------------------------------------
SELECT m.id AS movement_id, m.number, m.recipient_customer_id, m.organization_id
FROM movements m
WHERE m.id = 9558;                         -- 1 linha: 9558, 120497, 32, 1311

SELECT mi.id AS movement_item_id, mi.equipment_id, e.number AS tombo
FROM movement_items mi
JOIN equipments e ON e.id = mi.equipment_id
WHERE mi.movement_id = 9558
ORDER BY mi.id;                            -- 3 linhas: 40491/44764, 40492/45175, 40493/45178

SELECT COUNT(*) AS historico_44764
FROM equipment_history
WHERE id = 120673
  AND equipment_id = 44764
  AND movement_item_id = 40491
  AND service_order_item_id = 40491
  AND shipment_item_id = 40491;              -- 1 (SHIPPING_CONFIRMED_SEPARATE)

SELECT
  (SELECT COUNT(*) FROM romaneios           WHERE shipment_id = 40491) AS romaneios,
  (SELECT COUNT(*) FROM shipment_documents  WHERE shipment_id = 40491) AS shipment_documents,
  (SELECT COUNT(*) FROM shipment_items      WHERE shipment_id = 40491) AS shipment_items,
  (SELECT COUNT(*) FROM shipment_movements  WHERE shipment_id = 40491) AS shipment_movements,
  (SELECT COUNT(*) FROM volume_items WHERE shipment_item_id = 40491)   AS volume_items_por_shipment_item;
  -- esperado: 0, 0, 1, 1, 0

SELECT id, last_movement_item_customer_id
FROM equipments
WHERE id = 44764;                          -- last_movement_item_customer_id = 40491


-- -----------------------------------------------------------------------------
-- 2) BACKUP SELETIVO (guarda exatamente as linhas que serão removidas)
-- -----------------------------------------------------------------------------
CREATE TABLE bk_limp_9558_movements AS
SELECT * FROM movements WHERE id = 9558;

CREATE TABLE bk_limp_9558_movement_items AS
SELECT * FROM movement_items WHERE id = 40491 AND equipment_id = 44764;

CREATE TABLE bk_limp_9558_service_order_items AS
SELECT * FROM service_order_items WHERE id = 40491 AND equipment_id = 44764;

CREATE TABLE bk_limp_9558_shipments AS
SELECT * FROM shipments WHERE id = 40491;

CREATE TABLE bk_limp_9558_shipment_movements AS
SELECT * FROM shipment_movements WHERE shipment_id = 40491 AND movement_id = 9558;

CREATE TABLE bk_limp_9558_shipment_items AS
SELECT * FROM shipment_items WHERE id = 40491 AND movement_item_id = 40491;

CREATE TABLE bk_limp_9558_volume_items AS
SELECT * FROM volume_items WHERE id = 42156 AND movement_item_id = 40491;

CREATE TABLE bk_limp_9558_equipment_history AS
SELECT * FROM equipment_history
WHERE id = 120673 AND equipment_id = 44764 AND movement_item_id = 40491;

CREATE TABLE bk_limp_9558_equipment_44764 AS
SELECT id, status_id, address_id, last_movement_item_customer_id
FROM equipments WHERE id = 44764;


-- -----------------------------------------------------------------------------
-- 3) LIMPEZA (transação; avaliar o resultado e decidir COMMIT ou ROLLBACK)
-- -----------------------------------------------------------------------------
START TRANSACTION;

-- 3.1 histórico do equipamento (filho de movement_items)
DELETE FROM equipment_history
WHERE id = 120673
  AND equipment_id = 44764
  AND movement_item_id = 40491;            -- esperado: 1

-- 3.2 vínculo do volume (o volume 9558 continua com os outros 2 itens)
DELETE FROM volume_items
WHERE id = 42156
  AND movement_item_id = 40491;            -- esperado: 1

-- 3.3 guia de transporte do item
DELETE FROM shipment_items
WHERE id = 40491
  AND movement_item_id = 40491;            -- esperado: 1

DELETE FROM shipment_movements
WHERE shipment_id = 40491
  AND movement_id = 9558;                  -- esperado: 1

DELETE FROM shipments
WHERE id = 40491;                           -- esperado: 1

-- 3.4 item do movimento e item do pedido
DELETE FROM movement_items
WHERE id = 40491
  AND movement_id = 9558
  AND equipment_id = 44764;                -- esperado: 1

DELETE FROM service_order_items
WHERE id = 40491
  AND service_order_id = 9558
  AND equipment_id = 44764;                -- esperado: 1

-- 3.5 ponteiro do equipamento para o item removido
UPDATE equipments
SET last_movement_item_customer_id = NULL
WHERE id = 44764
  AND last_movement_item_customer_id = 40491;  -- esperado: 1

-- -----------------------------------------------------------------------------
-- 4) PÓS-CHECK (dentro da mesma transação, antes do COMMIT)
-- -----------------------------------------------------------------------------
SELECT COUNT(*) AS itens_restantes_9558
FROM movement_items WHERE movement_id = 9558;            -- esperado: 2 (45175, 45178)

SELECT COUNT(*) AS itens_44764
FROM movement_items WHERE equipment_id = 44764;          -- esperado: 0

SELECT COUNT(*) AS historico_orfao
FROM equipment_history WHERE id = 120673;                -- esperado: 0

SELECT COUNT(*) AS ponteiro_pendente
FROM equipments
WHERE id = 44764 AND last_movement_item_customer_id = 40491;  -- esperado: 0

SELECT COUNT(*) AS volume_items_restantes
FROM volume_items WHERE volume_id = 9558;                -- esperado: 2

SELECT
  (SELECT COUNT(*) FROM movement_items    WHERE id = 40491) AS movement_item_40491,
  (SELECT COUNT(*) FROM service_order_items WHERE id = 40491) AS service_order_item_40491,
  (SELECT COUNT(*) FROM shipments         WHERE id = 40491) AS shipment_40491,
  (SELECT COUNT(*) FROM shipment_items    WHERE id = 40491) AS shipment_item_40491,
  (SELECT COUNT(*) FROM shipment_movements WHERE shipment_id = 40491) AS shipment_movement_40491;
  -- esperado: 0, 0, 0, 0, 0

SELECT COUNT(*) AS capa_9558_intacta
FROM movements WHERE id = 9558 AND number = '120497' AND recipient_customer_id = 32;
-- esperado: 1

-- Se tudo conferir:
-- COMMIT;
-- Caso contrário:
-- ROLLBACK;


-- -----------------------------------------------------------------------------
-- 5) ROLLBACK MANUAL (se o COMMIT já tiver ocorrido e for preciso desfazer)
-- -----------------------------------------------------------------------------
-- INSERT INTO equipment_history SELECT * FROM bk_limp_9558_equipment_history;
-- INSERT INTO volume_items SELECT * FROM bk_limp_9558_volume_items;
-- INSERT INTO shipment_items SELECT * FROM bk_limp_9558_shipment_items;
-- INSERT INTO shipment_movements SELECT * FROM bk_limp_9558_shipment_movements;
-- INSERT INTO shipments SELECT * FROM bk_limp_9558_shipments;
-- INSERT INTO movement_items SELECT * FROM bk_limp_9558_movement_items;
-- INSERT INTO service_order_items SELECT * FROM bk_limp_9558_service_order_items;
-- UPDATE equipments e
--   JOIN bk_limp_9558_equipment_44764 b ON b.id = e.id
--   SET e.status_id = b.status_id,
--       e.address_id = b.address_id,
--       e.last_movement_item_customer_id = b.last_movement_item_customer_id
-- WHERE e.id = 44764;


-- -----------------------------------------------------------------------------
-- 6) DEPOIS DA LIMPEZA
-- -----------------------------------------------------------------------------
-- O ajuste passa a poder reescrever o equipamento 44764 pelo legado:
--   .venv/bin/python main.py ajustes_movimentos --aplicar --movimento 552854
-- Resultado esperado (simulado em transação com rollback em 2026-11-14):
--   44764 -> movimento 552854 (SUBSTITUIÇÃO, 2026-04-24), cliente 2721,
--            status 8 (EM DEVOLUÇÃO), address 2375, equipment_history novo.
--            Nenhum item residual do 44764 em 9558.
--
--   45174 -> o ajuste geral também corrige este equipamento (é elegível):
--            cria um movimento próprio número 120497 tipo ALUGUEL, cliente 32,
--            status 2, address 171, item novo para 45174.
--
--   Por que um movimento separado para o 45174: `_obter_cabecalho` não reusa
--   a capa 9558 quando o `movement_type_id` diverge (9558 é AVULSO/7 e o
--   legado 120497 do 45174 é ALUGUEL/1). O número 120497 fica repetido em
--   duas capas de tipos diferentes — é o comportamento previsto pela regra
--   "número reaproveitado para outro tipo de operação: não misturar".
--
--   A capa 9558 permanece AVULSO com os itens 45175 e 45178.
