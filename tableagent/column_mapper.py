def map_columns(df, catalog):
    slots = {
        'col_product': ['produto', 'product', 'item', 'sku'],
        'col_amount': ['total', 'valor', 'revenue', 'amount', 'r$_total'],
        'col_date': ['data', 'date', 'periodo'],
        'col_date_start': ['inicio', 'start', 'data_inicio', 'dt_inicio', 'previsao_inicio'],
        'col_date_end': ['fim', 'end', 'data_fim', 'dt_fim', 'termino', 'previsao_fim'],
        'col_label': ['modulo', 'nome', 'name', 'atividade', 'tarefa', 'etapa', 'descricao', 'titulo'],
        'col_customer': ['cliente', 'customer', 'comprador'],
        'col_quantity': ['quant', 'qty', 'quantidade'],
        'col_unit_price': ['unitario', 'unit_price', 'preco']
    }
    
    mapping = {}
    cols_lower = {col: col.lower().strip().replace(' ', '_') for col in df.columns}
    
    for slot, keywords in slots.items():
        for col, col_norm in cols_lower.items():
            if any(kw in col_norm for kw in keywords):
                mapping[slot] = col
                break
    
    return mapping
