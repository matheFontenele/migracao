-- ============================================================================
-- EXPORT EQUIPAMENTOS ESPECÍFICOS (lista de tombos) → CSV
-- Para importar no DataGrip destino
-- ============================================================================
-- Tombos fornecidos: 98492, 98493, 98494, 98495, 98496, 98497, 98498, 98499, 98500,
--     106031, 98601, 106032, 106033, 98602, 106034, 106035, 106036,
--     106037, 98603, 106038, 106039, 106040, 106041, 98604, 98605,
--     98606, 98607, 98608, 106042, 106044, 106045, 106043, 106046,
--     106047, 98609, 98610, 106048, 106049, 106050, 106051, 98611,
--     98612, 98613, 106052, 106053, 105962, 105963, 105964, 105965,
--     105966, 105967, 105970, 105971, 105972, 105968, 105969, 105973,
--     105974, 105975, 102001, 105976, 105977, 105978, 102002, 105979,
--     105980, 105981, 105984, 105985, 105982, 105983, 105987, 105986,
--     105994, 105989, 105995, 105992, 105988, 105991, 105990, 105996,
--     105993, 105998, 105997, 105999, 106000, 106001, 106002, 106004,
--     106003, 106005, 106008, 106010, 106006, 106007, 106009, 106012,
--     106015, 106016, 106013, 106014, 106011, 106017, 106018, 106022,
--     106021, 106020, 106019, 106028, 106024, 106026, 106023, 106027,
--     106025, 106030, 106029, 104370, 48214, 78868, 77245, 88171,
--     91761, 91772, 96575, 91480, 75064, 85151, 96576, 57794,
--     96578, 96581, 96577

SELECT
    -- Equipamento principal
    e.id                                    AS equipment_id,
    e.number                                AS tombo,
    e.name                                  AS equipamento_nome,
    e.serial_number                         AS numero_serie,
    e.serial_required                       AS serial_obrigatorio,

    -- Status
    s.name                                  AS status_nome,
    e.status_id                             AS status_id,

    -- Organização (atual)
    o.id                                    AS organizacao_id,
    o.name                                  AS organizacao_nome,
    o.alias                                 AS organizacao_alias,
    o.cpf_cnpj                              AS organizacao_cnpj,

    -- Endereço do equipamento
    a.id                                    AS endereco_id,
    a.alias                                 AS endereco_alias,
    a.street                                AS endereco_rua,
    a.number                                AS endereco_numero,
    a.details                               AS endereco_complemento,
    a.district                              AS endereco_bairro,
    a.city                                  AS endereco_cidade,
    a.state                                 AS endereco_uf,
    a.zip                                   AS endereco_cep,
    a.country                               AS endereco_pais,

    -- Product Item (estoque)
    pi.id                                   AS product_item_id,
    pi.code                                 AS product_item_codigo,
    pi.quantity                             AS product_item_quantidade,
    pi.average_cost                         AS product_item_custo_medio,
    pi.category_id                          AS product_item_categoria_id,
    pi.condition_id                         AS product_item_condicao_id,

    -- Product (definição do modelo)
    p.id                                    AS product_id,
    p.name                                  AS product_nome,
    p.is_asset                              AS product_is_asset,
    p.length                                AS product_comprimento,
    p.width                                 AS product_largura,
    p.height                                AS product_altura,
    p.weight                                AS product_peso,
    p.min_quantity                          AS product_qtd_min,
    p.max_quantity                          AS product_qtd_max,

    -- Brand / Type / Group
    b.id                                    AS brand_id,
    b.name                                  AS brand_nome,
    t.id                                    AS type_id,
    t.name                                  AS type_nome,
    t.is_kit                                AS type_is_kit,
    g.id                                    AS group_id,
    g.name                                  AS group_nome,

    -- Transação de origem (compra)
    tr.id                                   AS transaction_id,
    tr.transaction_date                     AS transaction_data,
    tr.transaction_type_id                  AS transaction_tipo_id,
    tr.doc_number                           AS transaction_nota_fiscal,
    tr.doc_date                             AS transaction_data_doc,
    tr.purchase_date                        AS transaction_data_compra,
    tr.amount_total                         AS transaction_valor_total,
    tr.amount_discount                      AS transaction_desconto,
    tr.details                              AS transaction_obs,
    tr.supplier_id                          AS transaction_fornecedor_id,
    tr.buyer_id                             AS transaction_comprador_org_id,

    -- Transaction Item (ligação equipamento-transação)
    ti.id                                   AS transaction_item_id,
    ti.unit_cost                            AS ti_custo_unitario,
    ti.quantity                             AS ti_quantidade,
    ti.warranty_date                        AS ti_data_garantia,
    ti.category_id                          AS ti_categoria_id,
    ti.condition_id                         AS ti_condicao_id,

    -- Fornecedor
    sup.id                                  AS supplier_id,
    sup.name                                AS supplier_nome,
    sup.alias                               AS supplier_alias,
    sup.cpf_cnpj                            AS supplier_cnpj,
    sup.phone                               AS supplier_telefone,
    sup.email                               AS supplier_email,

    -- Auditoria
    e.created_at                            AS equip_created_at,
    e.updated_at                            AS equip_updated_at,
    e.deleted_at                            AS equip_deleted_at,
    e.is_completed                          AS equip_completed,
    e.movement_date                         AS equip_movement_date,
    e.last_movement_item_customer_id        AS equip_ultimo_cliente_movimento

FROM equipments e

-- Status
INNER JOIN status s ON s.id = e.status_id

-- Organização atual
INNER JOIN organizations o ON o.id = e.current_organization_id

-- Endereço do equipamento
LEFT JOIN addresses a ON a.id = e.address_id AND a.deleted_at IS NULL

-- Product Item
INNER JOIN product_items pi ON pi.id = e.product_item_id

-- Product
INNER JOIN products p ON p.id = pi.product_id

-- Brand / Type / Group
LEFT JOIN brands b ON b.id = p.brand_id
LEFT JOIN types t ON t.id = p.type_id
LEFT JOIN `groups` g ON g.id = p.group_id

-- Transaction Item
LEFT JOIN transaction_items ti ON ti.id = e.transaction_item_id

-- Transaction
LEFT JOIN transactions tr ON tr.id = ti.transaction_id

-- Supplier (fornecedor da transação)
LEFT JOIN suppliers sup ON sup.id = tr.supplier_id

WHERE e.number IN (
    98492, 98493, 98494, 98495, 98496, 98497, 98498, 98499, 98500,
    106031, 98601, 106032, 106033, 98602, 106034, 106035, 106036,
    106037, 98603, 106038, 106039, 106040, 106041, 98604, 98605,
    98606, 98607, 98608, 106042, 106044, 106045, 106043, 106046,
    106047, 98609, 98610, 106048, 106049, 106050, 106051, 98611,
    98612, 98613, 106052, 106053, 105962, 105963, 105964, 105965,
    105966, 105967, 105970, 105971, 105972, 105968, 105969, 105973,
    105974, 105975, 102001, 105976, 105977, 105978, 102002, 105979,
    105980, 105981, 105984, 105985, 105982, 105983, 105987, 105986,
    105994, 105989, 105995, 105992, 105988, 105991, 105990, 105996,
    105993, 105998, 105997, 105999, 106000, 106001, 106002, 106004,
    106003, 106005, 106008, 106010, 106006, 106007, 106009, 106012,
    106015, 106016, 106013, 106014, 106011, 106017, 106018, 106022,
    106021, 106020, 106019, 106028, 106024, 106026, 106023, 106027,
    106025, 106030, 106029, 104370, 48214, 78868, 77245, 88171,
    91761, 91772, 96575, 91480, 75064, 85151, 96576, 57794,
    96578, 96581, 96577
)
ORDER BY e.number;
