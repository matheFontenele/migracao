-- ============================================================================
-- MIGRAÇÃO COMPLETA: EQUIPAMENTOS + DIMENSÕES (SQL PURO)
-- Rode no banco DESTINO (refatorado) - MySQL 8+
-- ============================================================================
-- Tombos alvo: 132 equipamentos
-- ============================================================================

SET FOREIGN_KEY_CHECKS = 0;
SET SQL_MODE = 'NO_AUTO_VALUE_ON_ZERO';
SET time_zone = '+00:00';

-- ============================================================================
-- VARIÁVEIS (ajuste se necessário)
-- ============================================================================
SET @TOMBO_LIST = '98492,98493,98494,98495,98496,98497,98498,98499,98500,
    106031,98601,106032,106033,98602,106034,106035,106036,
    106037,98603,106038,106039,106040,106041,98604,98605,
    98606,98607,98608,106042,106044,106045,106043,106046,
    106047,98609,98610,106048,106049,106050,106051,98611,
    98612,98613,106052,106053,105962,105963,105964,105965,
    105966,105967,105970,105971,105972,105968,105969,105973,
    105974,105975,102001,105976,105977,105978,102002,105979,
    105980,105981,105984,105985,105982,105983,105987,105986,
    105994,105989,105995,105992,105988,105991,105990,105996,
    105993,105998,105997,105999,106000,106001,106002,106004,
    106003,106005,106008,106010,106006,106007,106009,106012,
    106015,106016,106013,106014,106011,106017,106018,106022,
    106021,106020,106019,106028,106024,106026,106023,106027,
    106025,106030,106029,104370,48214,78868,77245,88171,
    91761,91772,96575,91480,75064,85151,96576,57794,
    96578,96581,96577';

-- ============================================================================
-- 1. ORGANIZATIONS (IDs fixos conhecidos)
-- ============================================================================
INSERT IGNORE INTO `organizations` (`id`, `alias`, `name`, `cpf_cnpj`, `phone`, `email`, `color`, `active`, `created_at`, `updated_at`, `deleted_at`) VALUES
(1115, 'ALUCOM', 'ALUCOM', '01628251000188', NULL, NULL, '#000000', 1, NOW(), NOW(), NULL),
(1122, 'MOREIA', 'MOREIA', '00000000000000', NULL, NULL, '#000000', 1, NOW(), NOW(), NULL),
(1311, 'IP', 'IP', '31810764000176', NULL, NULL, '#000000', 1, NOW(), NOW(), NULL),
(1378, 'AS SISTEMAS', 'AS SISTEMAS', '00000000000000', NULL, NULL, '#000000', 1, NOW(), NOW(), NULL);

-- ============================================================================
-- 2. ADDRESSES (organization bases)
-- ============================================================================
INSERT IGNORE INTO `addresses` (`id`, `addressable_type`, `addressable_id`, `alias`, `zip`, `street`, `number`, `details`, `district`, `city`, `state`, `country`, `contact_name`, `contact_phone`, `contact_email`, `legacy_customer_id`, `reserved_customer_id`, `created_at`, `updated_at`, `deleted_at`) VALUES
(1, 'organization', 1115, 'ALUCOM - BASE', '60175205', 'RUA RIACHUELO PAPICU', '40', NULL, NULL, 'FORTALEZA', 'CE', 'Brasil', NULL, NULL, NULL, NULL, NULL, NOW(), NOW(), NULL),
(2, 'organization', 1122, 'MOREIA - BASE', '60175205', 'RUA RIACHUELO PAPICU', '50', NULL, NULL, 'FORTALEZA', 'CE', 'Brasil', NULL, NULL, NULL, NULL, NULL, NOW(), NOW(), NULL),
(3, 'organization', 1311, 'IP - BASE', '60175205', 'RUA RIACHUELO PAPICU', '60', NULL, NULL, 'FORTALEZA', 'CE', 'Brasil', NULL, NULL, NULL, NULL, NULL, NOW(), NOW(), NULL),
(4, 'organization', 1378, 'AS SISTEMAS - BASE', '60175205', 'RUA RIACHUELO PAPICU', '70', NULL, NULL, 'FORTALEZA', 'CE', 'Brasil', NULL, NULL, NULL, NULL, NULL, NOW(), NOW(), NULL);

-- ============================================================================
-- 3. STATUS (valores padrão do sistema)
-- ============================================================================
INSERT IGNORE INTO `status` (`id`, `name`, `created_at`, `updated_at`, `deleted_at`) VALUES
(1, 'DISPONÍVEL PARA LOCAÇÃO', NOW(), NOW(), NULL),
(2, 'ALUGADO', NOW(), NOW(), NULL),
(3, 'RESERVADO', NOW(), NOW(), NULL),
(4, 'EMPRESTADO', NOW(), NOW(), NULL),
(5, 'USO INTERNO', NOW(), NOW(), NULL),
(6, 'EM MANUTENÇÃO', NOW(), NOW(), NULL),
(7, 'NA GARANTIA', NOW(), NOW(), NULL),
(8, 'EM DEVOLUÇÃO', NOW(), NOW(), NULL),
(9, 'BAIXADO', NOW(), NOW(), NULL),
(10, 'AGUARDANDO CARREGAMENTO', NOW(), NOW(), NULL),
(11, 'EM ROTA', NOW(), NOW(), NULL),
(12, 'AGUARDANDO MOVIMENTAÇÃO', NOW(), NOW(), NULL),
(13, 'AGUARDANDO SUBSTITUIÇÃO', NOW(), NOW(), NULL),
(15, 'RETIRADO DO CARREGAMENTO', NOW(), NOW(), NULL),
(16, 'LIBERADO', NOW(), NOW(), NULL);

-- ============================================================================
-- TABELAS TEMPORÁRIAS PARA CARREGAR DADOS DA ORIGEM (substitua pelos CSVs ou LOAD DATA)
-- ============================================================================
-- IMPORTANTE: Antes de rodar o bloco abaixo, carregue os CSVs exportados da origem
-- nas tabelas temporárias abaixo (ou use LOAD DATA LOCAL INFILE).
-- Exemplo de estrutura esperada:

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_suppliers` (
  `id` BIGINT, `alias` VARCHAR(255), `name` VARCHAR(255), `cpf_cnpj` VARCHAR(14),
  `phone` VARCHAR(20), `email` VARCHAR(255), `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_brands` (
  `id` BIGINT, `name` VARCHAR(255), `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_types` (
  `id` BIGINT, `name` VARCHAR(255), `is_kit` TINYINT, `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_groups` (
  `id` BIGINT, `name` VARCHAR(255), `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_products` (
  `id` BIGINT, `name` VARCHAR(255), `brand_id` BIGINT, `type_id` BIGINT, `group_id` BIGINT,
  `is_asset` TINYINT, `length` INT, `width` INT, `height` INT, `weight` DOUBLE,
  `min_quantity` INT, `max_quantity` INT, `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_product_items` (
  `id` BIGINT, `code` VARCHAR(255), `product_id` BIGINT, `category_id` BIGINT, `condition_id` BIGINT,
  `address_id` BIGINT, `location_id` BIGINT, `organization_id` BIGINT,
  `average_cost` DECIMAL(9,4), `quantity` DOUBLE, `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_transactions` (
  `id` BIGINT, `transaction_date` TIMESTAMP, `transaction_type_id` BIGINT, `supplier_id` BIGINT,
  `buyer_id` BIGINT, `sender_id` BIGINT, `sender_address_id` BIGINT, `receiver_address_id` BIGINT,
  `receiver_id` BIGINT, `customer_id` BIGINT, `doc_type_id` BIGINT, `doc_number` VARCHAR(255),
  `doc_date` TIMESTAMP, `purchase_date` DATE, `created_by` BIGINT, `details` VARCHAR(255),
  `is_internal_transfer` TINYINT, `is_internal_exit` TINYINT, `department` VARCHAR(100),
  `internal_source_transaction_id` BIGINT, `amount_total` DECIMAL(9,4), `amount_discount` DECIMAL(9,4),
  `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_transaction_items` (
  `id` BIGINT, `transaction_id` BIGINT, `product_id` BIGINT, `equipment_id` BIGINT,
  `category_id` BIGINT, `condition_id` BIGINT, `warranty_date` DATE,
  `address_id` BIGINT, `location_id` BIGINT, `unit_cost` DECIMAL(9,4), `quantity` INT,
  `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_equipments` (
  `id` BIGINT, `product_item_id` BIGINT, `transaction_item_id` BIGINT, `number` INT,
  `name` VARCHAR(255), `serial_number` VARCHAR(255), `serial_required` TINYINT,
  `current_organization_id` BIGINT, `status_id` BIGINT, `address_id` BIGINT,
  `location_id` BIGINT, `is_completed` TINYINT, `completed_by` BIGINT,
  `movement_date` TIMESTAMP, `last_movement_item_customer_id` BIGINT,
  `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP,
  `reserved_type` VARCHAR(255), `reserved_id` BIGINT
);

CREATE TEMPORARY TABLE IF NOT EXISTS `tmp_equipment_history` (
  `id` BIGINT, `equipment_id` BIGINT, `status_id` INT, `occurred_at` TIMESTAMP,
  `movement_item_id` BIGINT, `service_order_item_id` BIGINT, `contract_item_id` BIGINT,
  `shipment_item_id` BIGINT, `is_conversion` TINYINT, `reason` VARCHAR(255),
  `user_id` BIGINT, `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `deleted_at` TIMESTAMP,
  `reserved_type` VARCHAR(255), `reserved_id` BIGINT
);

-- ============================================================================
-- CARREGUE OS DADOS NAS TEMP TABLES ANTES DE CONTINUAR
-- Exemplo:
-- LOAD DATA LOCAL INFILE '/path/tmp_suppliers.csv' INTO TABLE tmp_supplers ...
-- ============================================================================

-- ============================================================================
-- 4. SUPPLIERS (resolve por name/alias)
-- ============================================================================
INSERT IGNORE INTO `suppliers` (`id`, `alias`, `name`, `cpf_cnpj`, `phone`, `email`, `created_at`, `updated_at`, `deleted_at`)
SELECT `id`, `alias`, `name`, `cpf_cnpj`, `phone`, `email`, `created_at`, `updated_at`, `deleted_at`
FROM `tmp_suppliers`;

-- ============================================================================
-- 5. BRANDS
-- ============================================================================
INSERT IGNORE INTO `brands` (`id`, `name`, `created_at`, `updated_at`, `deleted_at`)
SELECT `id`, `name`, `created_at`, `updated_at`, `deleted_at`
FROM `tmp_brands`;

-- ============================================================================
-- 6. TYPES
-- ============================================================================
INSERT IGNORE INTO `types` (`id`, `name`, `is_kit`, `created_at`, `updated_at`, `deleted_at`)
SELECT `id`, `name`, `is_kit`, `created_at`, `updated_at`, `deleted_at`
FROM `tmp_types`;

-- ============================================================================
-- 6. GROUPS
-- ============================================================================
INSERT IGNORE INTO `groups` (`id`, `name`, `created_at`, `updated_at`, `deleted_at`)
SELECT `id`, `name`, `created_at`, `updated_at`, `deleted_at`
FROM `tmp_groups`;

-- ============================================================================
-- 7. PRODUCTS (resolve FKs por name)
-- ============================================================================
INSERT IGNORE INTO `products` (
  `id`, `name`, `brand_id`, `type_id`, `group_id`, `is_asset`,
  `length`, `width`, `height`, `weight`, `min_quantity`, `max_quantity`,
  `created_at`, `updated_at`, `deleted_at`
)
SELECT
  p.`id`, p.`name`,
  b.`id` as brand_id, t.`id` as type_id, g.`id` as group_id,
  p.`is_asset`, p.`length`, p.`width`, p.`height`, p.`weight`,
  p.`min_quantity`, p.`max_quantity`,
  p.`created_at`, p.`updated_at`, p.`deleted_at`
FROM `tmp_products` p
LEFT JOIN `brands` b ON b.`name` = (SELECT name FROM tmp_brands WHERE id = p.brand_id LIMIT 1)
LEFT JOIN `types` t ON t.`name` = (SELECT name FROM tmp_types WHERE id = p.type_id LIMIT 1)
LEFT JOIN `groups` g ON g.`name` = (SELECT name FROM tmp_groups WHERE id = p.group_id LIMIT 1);

-- ============================================================================
-- 8. PRODUCT_ITEMS (resolve por code)
-- ============================================================================
INSERT IGNORE INTO `product_items` (
  `id`, `code`, `product_id`, `category_id`, `condition_id`,
  `address_id`, `location_id`, `organization_id`, `average_cost`, `quantity`,
  `created_at`, `updated_at`, `deleted_at`
)
SELECT
  pi.`id`, pi.`code`,
  p.`id` as product_id,
  pi.`category_id`, pi.`condition_id`,
  pi.`address_id`, pi.`location_id`, pi.`organization_id`,
  pi.`average_cost`, pi.`quantity`,
  pi.`created_at`, pi.`updated_at`, pi.`deleted_at`
FROM `tmp_product_items` pi
LEFT JOIN `products` p ON p.`name` = (SELECT name FROM tmp_products WHERE id = pi.product_id LIMIT 1);

-- ============================================================================
-- 9. TRANSACTIONS
-- ============================================================================
INSERT IGNORE INTO `transactions` (
  `id`, `transaction_date`, `transaction_type_id`, `supplier_id`, `buyer_id`,
  `sender_id`, `sender_address_id`, `receiver_address_id`, `receiver_id`,
  `customer_id`, `doc_type_id`, `doc_number`, `doc_date`, `purchase_date`,
  `created_by`, `details`, `is_internal_transfer`, `is_internal_exit`,
  `department`, `internal_source_transaction_id`, `amount_total`, `amount_discount`,
  `created_at`, `updated_at`, `deleted_at`
)
SELECT
  tr.`id`, tr.`transaction_date`, tr.`transaction_type_id`,
  s.`id` as supplier_id, tr.`buyer_id`, tr.`sender_id`, tr.`sender_address_id`,
  tr.`receiver_address_id`, tr.`receiver_id`, tr.`customer_id`, tr.`doc_type_id`,
  tr.`doc_number`, tr.`doc_date`, tr.`purchase_date`, tr.`created_by`,
  tr.`details`, tr.`is_internal_transfer`, tr.`is_internal_exit`,
  tr.`department`, tr.`internal_source_transaction_id`, tr.`amount_total`, tr.`amount_discount`,
  tr.`created_at`, tr.`updated_at`, tr.`deleted_at
FROM `tmp_transactions` tr
LEFT JOIN `suppliers` s ON s.`alias` = tr.`supplier_alias`
  OR s.`name` = (SELECT name FROM tmp_suppliers WHERE id = tr.supplier_id LIMIT 1);

-- ============================================================================
-- 10. TRANSACTION_ITEMS (resolve por id se já existe, senão por natural keys)
-- ============================================================================
INSERT IGNORE INTO `transaction_items` (
  `id`, `transaction_id`, `product_id`, `equipment_id`, `category_id`, `condition_id`,
  `warranty_date`, `address_id`, `location_id`, `unit_cost`, `quantity`,
  `created_at`, `updated_at`, `deleted_at`
)
SELECT
  ti.`id`,
  tr.`id` as transaction_id,
  p.`id` as product_id,
  ti.`equipment_id`,
  ti.`category_id`, ti.`condition_id`, ti.`warranty_date`,
  ti.`address_id`, ti.`location_id`, ti.`unit_cost`, ti.`quantity`,
  ti.`created_at`, ti.`updated_at`, ti.`deleted_at`
FROM `tmp_transaction_items` ti
LEFT JOIN `transactions` tr ON tr.`id` = ti.transaction_id
LEFT JOIN `products` p ON p.`name` = (SELECT name FROM tmp_products WHERE id = ti.product_id LIMIT 1);

-- ============================================================================
-- 11. EQUIPMENTS (o principal - seus 132 tombos)
-- ============================================================================
INSERT IGNORE INTO `equipments` (
  `id`, `product_item_id`, `transaction_item_id`, `number`, `name`, `serial_number`,
  `serial_required`, `current_organization_id`, `status_id`, `address_id`,
  `location_id`, `is_completed`, `completed_by`, `movement_date`,
  `last_movement_item_customer_id`, `created_at`, `updated_at`, `deleted_at`,
  `reserved_type`, `reserved_id`
)
SELECT
  e.`id`,
  pi.`id` as product_item_id,
  ti.`id` as transaction_item_id,
  e.`number`, e.`name`, e.`serial_number`, e.`serial_required`,
  e.`current_organization_id`, e.`status_id`, e.`address_id`,
  e.`location_id`, e.`is_completed`, e.`completed_by`, e.`movement_date`,
  e.`last_movement_item_customer_id`,
  e.`created_at`, e.`updated_at`, e.`deleted_at`,
  e.`reserved_type`, e.`reserved_id`
FROM `tmp_equipments` e
LEFT JOIN `product_items` pi ON pi.`code` = (SELECT code FROM tmp_product_items WHERE id = e.product_item_id LIMIT 1)
LEFT JOIN `transaction_items` ti ON ti.`id` = e.transaction_item_id
WHERE e.`number` IN (@TOMBO_LIST) AND e.`serial_required` = 1;

-- ============================================================================
-- 12. EQUIPMENT_HISTORY
-- ============================================================================
INSERT IGNORE INTO `equipment_history` (
  `id`, `equipment_id`, `status_id`, `occurred_at`, `movement_item_id`,
  `service_order_item_id`, `contract_item_id`, `shipment_item_id`,
  `is_conversion`, `reason`, `user_id`, `created_at`, `updated_at`, `deleted_at`,
  `reserved_type`, `reserved_id`
)
SELECT
  eh.`id`,
  e.`id` as equipment_id,
  eh.`status_id`, eh.`occurred_at`, eh.`movement_item_id`,
  eh.`service_order_item_id`, eh.`contract_item_id`, eh.`shipment_item_id`,
  eh.`is_conversion`, eh.`reason`, eh.`user_id`,
  eh.`created_at`, eh.`updated_at`, eh.`deleted_at`,
  eh.`reserved_type`, eh.`reserved_id`
FROM `tmp_equipment_history` eh
LEFT JOIN `equipments` e ON e.`number` = (
  SELECT number FROM tmp_equipments WHERE id = eh.equipment_id LIMIT 1)
);

-- ============================================================================
-- LIMPEZA
-- ============================================================================
DROP TEMPORARY TABLE IF EXISTS tmp_suppliers, tmp_brands, tmp_types, tmp_groups,
  tmp_products, tmp_product_items, tmp_transactions, tmp_transaction_items,
  tmp_equipments, tmp_equipment_history;

SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================================
-- VERIFICAÇÃO FINAL
-- ============================================================================
SELECT 'organizations' as tabela, COUNT(*) as qtd FROM organizations WHERE id IN (1115,1122,1311,1378)
UNION ALL SELECT 'addresses', COUNT(*) FROM addresses WHERE addressable_type='organization' AND addressable_id IN (1115,1122,1311,1378)
UNION ALL SELECT 'status', COUNT(*) FROM status
UNION ALL SELECT 'suppliers', COUNT(*) FROM suppliers
UNION ALL SELECT 'brands', COUNT(*) FROM brands
UNION ALL SELECT 'types', COUNT(*) FROM types
UNION ALL SELECT 'groups', COUNT(*) FROM `groups`
UNION ALL SELECT 'products', COUNT(*) FROM products
UNION ALL SELECT 'product_items', COUNT(*) FROM product_items
UNION ALL SELECT 'transactions', COUNT(*) FROM transactions
UNION ALL SELECT 'transaction_items', COUNT(*) FROM transaction_items
UNION ALL SELECT 'equipments', COUNT(*) FROM equipments WHERE number IN (@TOMBO_LIST) AND serial_required = 1
UNION ALL SELECT 'equipment_history', COUNT(*) FROM equipment_history eh
  JOIN equipments e ON e.id = eh.equipment_id
  WHERE e.number IN (@TOMBO_LIST) AND e.serial_required = 1;
