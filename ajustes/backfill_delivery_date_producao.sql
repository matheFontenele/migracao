-- Backfill de delivery_date gerado localmente.
-- Escopo: todos os clientes
-- Registros atualizáveis encontrados localmente: 13407
-- Fonte da data: DATE(movements.created_at).
--
-- O script NÃO conecta à produção. Execute este arquivo na sessão do banco
-- de produção, confira updated_rows e finalize manualmente com COMMIT ou
-- ROLLBACK. Não feche a sessão antes de finalizar a transação.

START TRANSACTION;

UPDATE shipment_items AS si
INNER JOIN shipment_status AS ss ON ss.id = si.status_id
INNER JOIN shipments AS s ON s.id = si.shipment_id
INNER JOIN movement_items AS mi ON mi.id = si.movement_item_id
INNER JOIN movements AS m ON m.id = mi.movement_id
SET si.delivery_date = DATE(m.created_at)
WHERE si.delivery_date IS NULL
  AND si.deleted_at IS NULL
  AND ss.name = 'ENTREGUE'
  AND s.deleted_at IS NULL
  AND mi.deleted_at IS NULL
  AND mi.operation_type = 'AVULSO'
  AND m.deleted_at IS NULL
  AND si.created_at IS NOT NULL
  AND s.created_at IS NOT NULL
  AND m.created_at IS NOT NULL
  AND m.movement_date IS NOT NULL
  AND DATE(si.created_at) = DATE(s.created_at)
  AND DATE(s.created_at) = DATE(m.created_at)
  AND DATE(m.created_at) = DATE(m.movement_date);

SELECT ROW_COUNT() AS updated_rows;

-- Se updated_rows estiver correto, execute: COMMIT;
-- Se estiver diferente do esperado, execute: ROLLBACK;
