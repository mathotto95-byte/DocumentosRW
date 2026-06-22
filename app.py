import re
from io import BytesIO
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st


# =========================================================
# CONFIGURAÇÕES
# =========================================================
DOCUMENTOS_COMPOSICAO = [
    "Cavalo - CIV",
    "Cavalo - Cronotacógrafo",
    "Carreta 1 - CIV",
    "Carreta 1 - CIPP",
    "Carreta 1 - Aferição",
    "Carreta 2 - CIV",
    "Carreta 2 - CIPP",
    "Carreta 2 - Aferição",
]

COLUNAS_VISUAIS = [
    "Status geral",
    "Composição",
    "Placa Cavalo",
    "Placa Carreta 1",
    "Placa Carreta 2",
    "Documento em alerta",
    "Vencimento em alerta",
    "Dias alerta",
    "Próximo documento",
    "Próximo vencimento",
    "Dias próximo",
    *DOCUMENTOS_COMPOSICAO,
    "Documentos no filtro",
]


# =========================================================
# UTILIDADES
# =========================================================
def normalizar_texto(valor) -> str:
    """Remove acentos, espaços duplicados e padroniza em maiúsculas."""
    if pd.isna(valor):
        return ""
    texto = str(valor).strip().upper()
    mapa = str.maketrans(
        "ÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇáàâãäéèêëíìîïóòôõöúùûüç",
        "AAAAAEEEEIIIIOOOOOUUUUCaaaaaeeeeiiiiooooouuuuc",
    )
    texto = texto.translate(mapa)
    return re.sub(r"\s+", " ", texto).strip()


def limpar_placa(valor) -> str:
    return normalizar_texto(valor).replace(" ", "").replace("-", "")


def data_excel_serial(valor):
    try:
        numero = float(valor)
    except Exception:
        return pd.NaT
    if not (30000 <= numero <= 60000):
        return pd.NaT
    return pd.Timestamp(datetime(1899, 12, 30) + timedelta(days=numero)).normalize()


def converter_data(valor):
    """Aceita data do Excel, número serial ou texto dd/mm/aaaa."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return pd.NaT
    if isinstance(valor, pd.Timestamp):
        return valor.normalize()
    if isinstance(valor, datetime):
        return pd.Timestamp(valor).normalize()
    if isinstance(valor, date):
        return pd.Timestamp(valor).normalize()

    serial = data_excel_serial(valor)
    if not pd.isna(serial):
        return serial

    texto = str(valor).strip()
    if texto == "":
        return pd.NaT

    data = pd.to_datetime(texto, dayfirst=True, errors="coerce")
    if pd.isna(data):
        return pd.NaT
    return pd.Timestamp(data).normalize()


def classificar_laudo(valor) -> str | None:
    """Classifica os laudos do KMM para os documentos usados na base."""
    texto = normalizar_texto(valor)
    if "CIPP" in texto:
        return "CIPP"
    if "CIV" in texto:
        return "CIV"
    if "AFERICAO" in texto or "AFER" in texto:
        return "AFERIÇÃO"
    if "CRONOT" in texto:
        return "CRONOTACÓGRAFO"
    return None


def formatar_data(valor):
    data = converter_data(valor)
    if pd.isna(data):
        return ""
    return data.strftime("%d/%m/%Y")


def status_vencimento(vencimento, ref: pd.Timestamp, fim_semana: pd.Timestamp, fim_mes: pd.Timestamp) -> str:
    if pd.isna(vencimento):
        return "SEM DATA"
    if vencimento < ref:
        return "VENCIDO"
    if vencimento == ref:
        return "VENCE HOJE"
    if ref < vencimento <= fim_semana:
        return "VENCE NA SEMANA"
    if fim_semana < vencimento <= fim_mes:
        return "VENCE NO MÊS"
    return "OK"


def status_prioridade(status: str) -> int:
    """Ordena status mesmo quando o texto vier com o documento no final.
    Ex.: "VENCE HOJE: Carreta 1 - AFERIÇÃO" continua com prioridade VENCE HOJE.
    """
    base = normalizar_texto(str(status).split(":")[0])
    ordem = {
        "VENCIDO": 0,
        "VENCE HOJE": 1,
        "VENCE NA SEMANA": 2,
        "VENCE NO MES": 3,
        "OK": 4,
        "SEM DATA": 5,
    }
    return ordem.get(base, 9)


def label_documento(equipamento: str, placa: str, documento: str) -> str:
    placa_limpa = limpar_placa(placa)
    return f"{equipamento} {placa_limpa} - {documento}".strip()


def resumir_documentos(itens: list[dict], limite: int = 2, com_data: bool = True) -> str:
    partes = []
    for item in itens[:limite]:
        texto = label_documento(item.get("equipamento", ""), item.get("placa", ""), item.get("documento", ""))
        venc = item.get("vencimento")
        if com_data and venc is not None and not pd.isna(venc):
            texto += f" ({pd.Timestamp(venc).strftime('%d/%m/%Y')})"
        partes.append(texto)
    if len(itens) > limite:
        partes.append(f"+{len(itens) - limite} doc.")
    return "; ".join(partes)


def montar_status_composicao(documentos: list[tuple], ref: pd.Timestamp, fim_semana: pd.Timestamp, fim_mes: pd.Timestamp) -> dict:
    """Monta o status geral da composição com o documento de alerta e o próximo vencimento.

    A regra é dinâmica pela data de referência:
    - se o documento vence hoje, aparece em VENCE HOJE;
    - no dia seguinte, o mesmo documento passa para VENCIDO;
    - quando há vencidos, o painel também mostra o próximo documento futuro da composição.
    """
    itens = []
    for equipamento, placa, documento, vencimento, origem, coluna_data in documentos:
        if not placa or pd.isna(vencimento):
            continue
        vencimento = pd.Timestamp(vencimento).normalize()
        itens.append(
            {
                "equipamento": equipamento,
                "placa": limpar_placa(placa),
                "documento": documento,
                "vencimento": vencimento,
                "dias": int((vencimento - ref).days),
                "status": status_vencimento(vencimento, ref, fim_semana, fim_mes),
            }
        )

    if not itens:
        return {
            "Status categoria": "SEM DATA",
            "Status geral": "SEM DATA",
            "Documento em alerta": "",
            "Vencimento em alerta": pd.NaT,
            "Dias alerta": "",
            "Próximo documento": "",
            "Próximo vencimento": pd.NaT,
            "Dias próximo": "",
            "Vencimento mais próximo": pd.NaT,
            "Dias p/ vencer": "",
        }

    grupos = {
        "VENCIDO": sorted([i for i in itens if i["status"] == "VENCIDO"], key=lambda x: x["vencimento"]),
        "VENCE HOJE": sorted([i for i in itens if i["status"] == "VENCE HOJE"], key=lambda x: x["vencimento"]),
        "VENCE NA SEMANA": sorted([i for i in itens if i["status"] == "VENCE NA SEMANA"], key=lambda x: x["vencimento"]),
        "VENCE NO MÊS": sorted([i for i in itens if i["status"] == "VENCE NO MÊS"], key=lambda x: x["vencimento"]),
    }

    status_categoria = "OK"
    alertas = []
    for status in ["VENCIDO", "VENCE HOJE", "VENCE NA SEMANA", "VENCE NO MÊS"]:
        if grupos[status]:
            status_categoria = status
            alertas = grupos[status]
            break

    if not alertas:
        futuros_ok = sorted([i for i in itens if i["vencimento"] > ref], key=lambda x: x["vencimento"])
        alertas = futuros_ok[:1]

    alerta_principal = alertas[0] if alertas else None
    futuros = sorted([i for i in itens if i["vencimento"] > ref], key=lambda x: x["vencimento"])
    proximo = futuros[0] if futuros else None

    if status_categoria == "OK":
        status_geral = "OK"
    else:
        status_geral = f"{status_categoria}: {resumir_documentos(alertas, limite=2, com_data=True)}"

    return {
        "Status categoria": status_categoria,
        "Status geral": status_geral,
        "Documento em alerta": resumir_documentos(alertas, limite=3, com_data=False),
        "Vencimento em alerta": alerta_principal["vencimento"] if alerta_principal else pd.NaT,
        "Dias alerta": alerta_principal["dias"] if alerta_principal else "",
        "Próximo documento": resumir_documentos([proximo], limite=1, com_data=False) if proximo else "",
        "Próximo vencimento": proximo["vencimento"] if proximo else pd.NaT,
        "Dias próximo": proximo["dias"] if proximo else "",
        # Campos mantidos para compatibilidade com ordenação/exportação: representam o alerta principal.
        "Vencimento mais próximo": alerta_principal["vencimento"] if alerta_principal else pd.NaT,
        "Dias p/ vencer": alerta_principal["dias"] if alerta_principal else "",
    }


# =========================================================
# LEITURA DAS PLANILHAS
# =========================================================
def localizar_linha_cabecalho_kmm(df_bruto: pd.DataFrame) -> int:
    """Localiza a linha que contém Placa, Laudo e Data Vencimento."""
    for i in range(min(20, len(df_bruto))):
        linha = [normalizar_texto(x) for x in df_bruto.iloc[i].tolist()]
        tem_placa = "PLACA" in linha
        tem_laudo = "LAUDO" in linha
        tem_venc = "DATA VENCIMENTO" in linha or "VENCIMENTO" in linha
        if tem_placa and tem_laudo and tem_venc:
            return i
    raise ValueError("Não localizei o cabeçalho do KMM com Placa, Laudo e Data Vencimento.")


def criar_nomes_unicos(colunas):
    usados = {}
    novas = []
    for c in colunas:
        base = normalizar_texto(c) or "COLUNA"
        usados[base] = usados.get(base, 0) + 1
        if usados[base] == 1:
            novas.append(base)
        else:
            novas.append(f"{base}_{usados[base]}")
    return novas


def ler_kmm(arquivo, aba: str) -> pd.DataFrame:
    bruto = pd.read_excel(arquivo, sheet_name=aba, header=None, dtype=object)
    linha_cab = localizar_linha_cabecalho_kmm(bruto)
    colunas = criar_nomes_unicos(bruto.iloc[linha_cab].tolist())
    df = bruto.iloc[linha_cab + 1 :].copy()
    df.columns = colunas
    df = df.dropna(how="all")
    return df


def ler_base(arquivo, aba: str) -> pd.DataFrame:
    df = pd.read_excel(arquivo, sheet_name=aba, dtype=object)
    df = df.dropna(how="all")
    return df


# =========================================================
# REGRA PRINCIPAL
# =========================================================
def montar_indice_kmm(df_kmm: pd.DataFrame) -> dict[tuple[str, str], pd.Timestamp]:
    col_placa = "PLACA"
    col_laudo = "LAUDO"

    col_venc = None
    for candidato in ["DATA VENCIMENTO", "VENCIMENTO", "DATA DE VENCIMENTO"]:
        if candidato in df_kmm.columns:
            col_venc = candidato
            break

    obrigatorias = [col_placa, col_laudo]
    faltantes = [c for c in obrigatorias if c not in df_kmm.columns]
    if col_venc is None:
        faltantes.append("DATA VENCIMENTO")
    if faltantes:
        raise ValueError(f"Colunas obrigatórias não encontradas no KMM: {faltantes}")

    indice = {}
    for _, row in df_kmm.iterrows():
        placa = limpar_placa(row.get(col_placa))
        documento = classificar_laudo(row.get(col_laudo))
        vencimento = converter_data(row.get(col_venc))
        if not placa or not documento or pd.isna(vencimento):
            continue

        chave = (placa, documento)
        # Mantém o maior vencimento para considerar o laudo vigente.
        if chave not in indice or vencimento > indice[chave]:
            indice[chave] = vencimento
    return indice


def escolher_vencimento(indice_kmm: dict, placa: str, documento: str, fallback_base):
    placa = limpar_placa(placa)
    venc_kmm = indice_kmm.get((placa, documento)) if placa else pd.NaT
    if venc_kmm is not None and not pd.isna(venc_kmm):
        return venc_kmm, "KMM"

    venc_base = converter_data(fallback_base)
    if not pd.isna(venc_base):
        return venc_base, "BASE"

    return pd.NaT, ""


def menor_data(datas):
    validas = [d for d in datas if d is not None and not pd.isna(d)]
    return min(validas) if validas else pd.NaT


def montar_relatorios(
    df_base: pd.DataFrame,
    indice_kmm: dict,
    data_referencia: date,
):
    ref = pd.Timestamp(data_referencia).normalize()
    inicio_semana = ref - pd.Timedelta(days=ref.weekday())
    fim_semana = inicio_semana + pd.Timedelta(days=6)
    inicio_mes = pd.Timestamp(date(ref.year, ref.month, 1))
    if ref.month == 12:
        fim_mes = pd.Timestamp(date(ref.year, 12, 31))
    else:
        fim_mes = pd.Timestamp(date(ref.year, ref.month + 1, 1)) - pd.Timedelta(days=1)

    atualizada = []
    detalhe = []

    for _, row in df_base.iterrows():
        valores = row.tolist()
        if len(valores) < 8:
            continue

        cavalo = limpar_placa(valores[0])
        carreta1 = limpar_placa(valores[3]) if len(valores) > 3 else ""
        carreta2 = limpar_placa(valores[4]) if len(valores) > 4 else ""

        if not cavalo and not carreta1 and not carreta2:
            continue

        cav_civ, src_cav_civ = escolher_vencimento(indice_kmm, cavalo, "CIV", valores[1] if len(valores) > 1 else None)
        cav_crono, src_cav_crono = escolher_vencimento(indice_kmm, cavalo, "CRONOTACÓGRAFO", valores[2] if len(valores) > 2 else None)

        car1_civ, src_car1_civ = escolher_vencimento(indice_kmm, carreta1, "CIV", valores[5] if len(valores) > 5 else None)
        car2_civ, src_car2_civ = escolher_vencimento(indice_kmm, carreta2, "CIV", valores[5] if len(valores) > 5 else None)
        car1_cipp, src_car1_cipp = escolher_vencimento(indice_kmm, carreta1, "CIPP", valores[6] if len(valores) > 6 else None)
        car2_cipp, src_car2_cipp = escolher_vencimento(indice_kmm, carreta2, "CIPP", valores[6] if len(valores) > 6 else None)
        car1_afer, src_car1_afer = escolher_vencimento(indice_kmm, carreta1, "AFERIÇÃO", valores[7] if len(valores) > 7 else None)
        car2_afer, src_car2_afer = escolher_vencimento(indice_kmm, carreta2, "AFERIÇÃO", valores[7] if len(valores) > 7 else None)

        documentos = [
            ("Cavalo", cavalo, "CIV", cav_civ, src_cav_civ, "Cavalo - CIV"),
            ("Cavalo", cavalo, "CRONOTACÓGRAFO", cav_crono, src_cav_crono, "Cavalo - Cronotacógrafo"),
            ("Carreta 1", carreta1, "CIV", car1_civ, src_car1_civ, "Carreta 1 - CIV"),
            ("Carreta 1", carreta1, "CIPP", car1_cipp, src_car1_cipp, "Carreta 1 - CIPP"),
            ("Carreta 1", carreta1, "AFERIÇÃO", car1_afer, src_car1_afer, "Carreta 1 - Aferição"),
            ("Carreta 2", carreta2, "CIV", car2_civ, src_car2_civ, "Carreta 2 - CIV"),
            ("Carreta 2", carreta2, "CIPP", car2_cipp, src_car2_cipp, "Carreta 2 - CIPP"),
            ("Carreta 2", carreta2, "AFERIÇÃO", car2_afer, src_car2_afer, "Carreta 2 - Aferição"),
        ]

        composicao = f"{cavalo} + {carreta1} + {carreta2}".strip(" +")
        info_status = montar_status_composicao(documentos, ref, fim_semana, fim_mes)

        registro = {
            "Status categoria": info_status["Status categoria"],
            "Status geral": info_status["Status geral"],
            "Composição": composicao,
            "Placa Cavalo": cavalo,
            "Placa Carreta 1": carreta1,
            "Placa Carreta 2": carreta2,
            "Documento em alerta": info_status["Documento em alerta"],
            "Vencimento em alerta": info_status["Vencimento em alerta"],
            "Dias alerta": info_status["Dias alerta"],
            "Próximo documento": info_status["Próximo documento"],
            "Próximo vencimento": info_status["Próximo vencimento"],
            "Dias próximo": info_status["Dias próximo"],
            "Cavalo - CIV": cav_civ,
            "Cavalo - Cronotacógrafo": cav_crono,
            "Carreta 1 - CIV": car1_civ,
            "Carreta 1 - CIPP": car1_cipp,
            "Carreta 1 - Aferição": car1_afer,
            "Carreta 2 - CIV": car2_civ,
            "Carreta 2 - CIPP": car2_cipp,
            "Carreta 2 - Aferição": car2_afer,
            "Vencimento mais próximo": info_status["Vencimento mais próximo"],
            "Dias p/ vencer": info_status["Dias p/ vencer"],
            "Documentos no filtro": "",
        }
        atualizada.append(registro)

        for equipamento, placa, documento, vencimento, origem, coluna_data in documentos:
            if not placa or pd.isna(vencimento):
                continue
            dias_doc = int((vencimento - ref).days)
            status_doc = status_vencimento(vencimento, ref, fim_semana, fim_mes)
            detalhe.append(
                {
                    "Status": status_doc,
                    "Composição": composicao,
                    "Equipamento": equipamento,
                    "Placa": placa,
                    "Documento": documento,
                    "Vencimento": vencimento,
                    "Dias p/ vencer": dias_doc,
                    "Origem atualização": origem,
                    "Placa Cavalo": cavalo,
                    "Placa Carreta 1": carreta1,
                    "Placa Carreta 2": carreta2,
                    "Coluna na composição": coluna_data,
                }
            )

    df_atualizada = pd.DataFrame(atualizada)
    df_detalhe = pd.DataFrame(detalhe)

    if not df_atualizada.empty:
        df_atualizada["_ordem_status"] = df_atualizada["Status geral"].map(status_prioridade)
        df_atualizada = df_atualizada.sort_values(["_ordem_status", "Vencimento mais próximo", "Composição"]).drop(columns=["_ordem_status"])

    if not df_detalhe.empty:
        df_detalhe["_ordem_status"] = df_detalhe["Status"].map(status_prioridade)
        df_detalhe = df_detalhe.sort_values(["_ordem_status", "Vencimento", "Composição", "Documento"]).drop(columns=["_ordem_status"])

    return {
        "base_atualizada": df_atualizada,
        "detalhe_documentos": df_detalhe,
        "vencidos": filtrar_por_status(df_atualizada, df_detalhe, ["VENCIDO"]),
        "vencem_hoje": filtrar_por_status(df_atualizada, df_detalhe, ["VENCE HOJE"]),
        "vencem_semana": filtrar_por_status(df_atualizada, df_detalhe, ["VENCE NA SEMANA"]),
        "vencem_mes": filtrar_por_status(df_atualizada, df_detalhe, ["VENCE NO MÊS"]),
        "inicio_semana": inicio_semana,
        "fim_semana": fim_semana,
        "inicio_mes": inicio_mes,
        "fim_mes": fim_mes,
        "data_referencia": ref,
    }


def filtrar_por_status(df_base: pd.DataFrame, df_detalhe: pd.DataFrame, status_lista: list[str]) -> pd.DataFrame:
    if df_base.empty or df_detalhe.empty:
        return pd.DataFrame(columns=COLUNAS_VISUAIS)

    detalhe_filtrado = df_detalhe[df_detalhe["Status"].isin(status_lista)].copy()
    if detalhe_filtrado.empty:
        return pd.DataFrame(columns=COLUNAS_VISUAIS)

    def item_longo(row):
        return (
            f"{row['Equipamento']} {row['Placa']} - {row['Documento']}: "
            f"{pd.Timestamp(row['Vencimento']).strftime('%d/%m/%Y')} "
            f"({int(row['Dias p/ vencer'])} dias)"
        )

    def item_curto(row):
        return f"{row['Equipamento']} {row['Placa']} - {row['Documento']}"

    detalhe_filtrado["Item"] = detalhe_filtrado.apply(item_longo, axis=1)
    detalhe_filtrado["Item curto"] = detalhe_filtrado.apply(item_curto, axis=1)

    itens_por_comp = (
        detalhe_filtrado.groupby("Composição")["Item"]
        .apply(lambda s: "\n".join(s.tolist()))
        .reset_index(name="Documentos no filtro")
    )

    comp = df_base[df_base["Composição"].isin(itens_por_comp["Composição"])].copy()
    comp = comp.drop(columns=["Documentos no filtro"], errors="ignore").merge(itens_por_comp, on="Composição", how="left")

    def status_descritivo(grupo: pd.DataFrame) -> str:
        status = sorted(grupo["Status"].unique(), key=status_prioridade)[0]
        docs_status = grupo[grupo["Status"] == status].sort_values("Vencimento")
        labels = docs_status["Item curto"].tolist()
        texto = "; ".join(labels[:2])
        if len(labels) > 2:
            texto += f"; +{len(labels) - 2} doc."
        return f"{status}: {texto}" if texto else status

    status_minimo = detalhe_filtrado.groupby("Composição").apply(status_descritivo)
    doc_alerta = detalhe_filtrado.sort_values("Vencimento").groupby("Composição")["Item curto"].first()
    vencimento_filtro = detalhe_filtrado.groupby("Composição")["Vencimento"].min()
    dias_filtro = detalhe_filtrado.groupby("Composição")["Dias p/ vencer"].min()

    comp["Status geral"] = comp["Composição"].map(status_minimo).fillna(comp["Status geral"])
    comp["Documento em alerta"] = comp["Composição"].map(doc_alerta).fillna(comp.get("Documento em alerta", ""))
    # No recorte filtrado, o vencimento/dias exibidos representam o documento mais próximo dentro daquele filtro.
    comp["Vencimento em alerta"] = comp["Composição"].map(vencimento_filtro).fillna(comp.get("Vencimento em alerta", pd.NaT))
    comp["Dias alerta"] = comp["Composição"].map(dias_filtro).fillna(comp.get("Dias alerta", ""))
    comp["Vencimento mais próximo"] = comp["Vencimento em alerta"]
    comp["Dias p/ vencer"] = comp["Dias alerta"]

    comp["_ordem_status"] = comp["Status geral"].map(status_prioridade)
    comp = comp.sort_values(["_ordem_status", "Vencimento em alerta", "Composição"]).drop(columns=["_ordem_status"])
    return comp[COLUNAS_VISUAIS]


# =========================================================
# FILTROS DA INTERFACE
# =========================================================
def aplicar_filtros(df: pd.DataFrame, filtro_placa: str, filtro_documentos: list[str], filtro_equipamentos: list[str], status_opcoes: list[str] | None = None) -> pd.DataFrame:
    if df.empty:
        return df

    resultado = df.copy()

    if status_opcoes and "Todos" not in status_opcoes:
        col_status = "Status" if "Status" in resultado.columns else "Status geral"
        resultado = resultado[resultado[col_status].isin(status_opcoes)]

    if filtro_placa:
        placa = limpar_placa(filtro_placa)
        colunas_placa = [c for c in ["Placa", "Placa Cavalo", "Placa Carreta 1", "Placa Carreta 2", "Composição"] if c in resultado.columns]
        mask = pd.Series(False, index=resultado.index)
        for col in colunas_placa:
            mask = mask | resultado[col].astype(str).str.replace("-", "", regex=False).str.upper().str.contains(placa, na=False)
        resultado = resultado[mask]

    if filtro_documentos and "Todos" not in filtro_documentos and "Documento" in resultado.columns:
        resultado = resultado[resultado["Documento"].isin(filtro_documentos)]

    if filtro_equipamentos and "Todos" not in filtro_equipamentos and "Equipamento" in resultado.columns:
        resultado = resultado[resultado["Equipamento"].isin(filtro_equipamentos)]

    return resultado


def aplicar_filtros_composicao(df_comp: pd.DataFrame, df_detalhe_filtrado: pd.DataFrame) -> pd.DataFrame:
    if df_comp.empty:
        return df_comp
    if df_detalhe_filtrado.empty:
        return df_comp.iloc[0:0].copy()

    comps = df_detalhe_filtrado["Composição"].dropna().unique().tolist()
    return df_comp[df_comp["Composição"].isin(comps)].copy()


def dataframe_datas(df: pd.DataFrame) -> pd.DataFrame:
    """Mantém somente identificação da composição, placas, status e datas."""
    if df.empty:
        return df
    colunas = [c for c in COLUNAS_VISUAIS if c in df.columns]
    return df[colunas].copy()


def configurar_colunas_datas(df: pd.DataFrame):
    config = {}
    for col in df.columns:
        if "Vencimento" in col or col in DOCUMENTOS_COMPOSICAO:
            config[col] = st.column_config.DateColumn(col, format="DD/MM/YYYY")
    return config


# =========================================================
# EXPORTAÇÃO EXCEL
# =========================================================
def preparar_para_excel(df: pd.DataFrame) -> pd.DataFrame:
    saida = df.copy()
    for col in saida.columns:
        if pd.api.types.is_datetime64_any_dtype(saida[col]):
            saida[col] = saida[col].dt.strftime("%d/%m/%Y")
    return saida


def exportar_excel(relatorios: dict, data_referencia: date, df_filtrado_detalhe: pd.DataFrame | None = None, df_filtrado_comp: pd.DataFrame | None = None) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        resumo = pd.DataFrame(
            [
                ["Data de referência", pd.Timestamp(data_referencia).strftime("%d/%m/%Y")],
                ["Semana analisada", f"{relatorios['inicio_semana'].strftime('%d/%m/%Y')} a {relatorios['fim_semana'].strftime('%d/%m/%Y')}"],
                ["Mês analisado", f"{relatorios['inicio_mes'].strftime('%d/%m/%Y')} a {relatorios['fim_mes'].strftime('%d/%m/%Y')}"],
                ["Composições na base", len(relatorios["base_atualizada"])],
                ["Documentos analisados", len(relatorios["detalhe_documentos"])],
                ["Composições com documentos vencidos", len(relatorios["vencidos"])],
                ["Composições com documentos vencendo hoje", len(relatorios["vencem_hoje"])],
                ["Composições com documentos vencendo no restante da semana", len(relatorios["vencem_semana"])],
                ["Composições com documentos vencendo no mês", len(relatorios["vencem_mes"])],
                ["Regra", "KMM atualiza por Placa + Tipo de Laudo. A composição permanece em uma única linha."],
            ],
            columns=["Indicador", "Valor"],
        )

        abas = {
            "RESUMO": resumo,
            "VENCIDOS": preparar_para_excel(relatorios["vencidos"]),
            "VENCE_HOJE": preparar_para_excel(relatorios["vencem_hoje"]),
            "VENCEM_SEMANA": preparar_para_excel(relatorios["vencem_semana"]),
            "VENCEM_MES": preparar_para_excel(relatorios["vencem_mes"]),
            "BASE_DATAS": preparar_para_excel(dataframe_datas(relatorios["base_atualizada"])),
            "DETALHE_DOCUMENTOS": preparar_para_excel(relatorios["detalhe_documentos"]),
        }
        if df_filtrado_comp is not None:
            abas["COMPOSICOES_FILTRADAS"] = preparar_para_excel(dataframe_datas(df_filtrado_comp))
        if df_filtrado_detalhe is not None:
            abas["DETALHE_FILTRADO"] = preparar_para_excel(df_filtrado_detalhe)

        for nome_aba, df in abas.items():
            df.to_excel(writer, sheet_name=nome_aba, index=False)

        workbook = writer.book
        header_fmt = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#17365D", "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
        text_fmt = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True})
        int_fmt = workbook.add_format({"border": 1, "num_format": "0"})

        for sheet_name, df in abas.items():
            ws = writer.sheets[sheet_name]
            ws.freeze_panes(1, 0)
            for col_num, col_name in enumerate(df.columns):
                ws.write(0, col_num, col_name, header_fmt)
                largura = min(max(len(str(col_name)) + 3, 14), 32)
                if col_name in ["Composição", "Documentos no filtro", "Regra", "Valor"]:
                    largura = 42
                if col_name == "Documentos no filtro":
                    largura = 56
                ws.set_column(col_num, col_num, largura, text_fmt)
                if "Dias" in str(col_name):
                    ws.set_column(col_num, col_num, 12, int_fmt)
            if len(df) > 0 and len(df.columns) > 0:
                ws.autofilter(0, 0, len(df), len(df.columns) - 1)

    return output.getvalue()



# =========================================================
# INTERFACE ONLINE - STREAMLIT V3
# =========================================================
st.set_page_config(page_title="Painel de Vencimentos", layout="wide", initial_sidebar_state="collapsed")

st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1.4rem;
            padding-bottom: 1.5rem;
            max-width: 1500px;
        }
        .main-title {
            font-size: 34px;
            font-weight: 800;
            color: #17365D;
            line-height: 1.1;
            margin-bottom: 2px;
        }
        .subtitle {
            color: #667085;
            font-size: 15px;
            margin-bottom: 16px;
        }
        .section-card {
            border: 1px solid #E6EAF0;
            border-radius: 18px;
            padding: 18px 18px 8px 18px;
            background: #FFFFFF;
            box-shadow: 0 4px 16px rgba(16, 24, 40, 0.05);
            margin-bottom: 14px;
        }
        .metric-card {
            border-radius: 18px;
            padding: 16px 16px 14px 16px;
            border: 1px solid #EAECF0;
            background: linear-gradient(180deg, #FFFFFF 0%, #F9FAFB 100%);
            box-shadow: 0 4px 14px rgba(16, 24, 40, 0.06);
            min-height: 105px;
        }
        .metric-label {
            font-size: 13px;
            font-weight: 700;
            color: #667085;
            text-transform: uppercase;
            letter-spacing: .03em;
            margin-bottom: 7px;
        }
        .metric-value {
            font-size: 31px;
            font-weight: 850;
            color: #101828;
            line-height: 1.05;
        }
        .metric-note {
            font-size: 12px;
            color: #667085;
            margin-top: 7px;
        }
        .danger { border-left: 6px solid #B42318; }
        .warning { border-left: 6px solid #DC6803; }
        .month { border-left: 6px solid #2E90FA; }
        .neutral { border-left: 6px solid #475467; }
        .ok { border-left: 6px solid #039855; }
        .small-caption {
            color: #667085;
            font-size: 13px;
            margin-top: -4px;
            margin-bottom: 6px;
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid #EAECF0;
            border-radius: 14px;
            overflow: hidden;
        }
        .stDownloadButton > button {
            border-radius: 12px;
            font-weight: 700;
            border: 1px solid #17365D;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def metric_card(label: str, value, note: str = "", css_class: str = "neutral"):
    st.markdown(
        f"""
        <div class="metric-card {css_class}">
            <div class="metric-label">{label}</div>
            <div class="metric-value">{value}</div>
            <div class="metric-note">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def formatar_periodo(relatorios: dict, data_ref: date) -> str:
    return (
        f"Referência: {pd.Timestamp(data_ref).strftime('%d/%m/%Y')} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"Semana: {relatorios['inicio_semana'].strftime('%d/%m/%Y')} a {relatorios['fim_semana'].strftime('%d/%m/%Y')} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"Mês: {relatorios['inicio_mes'].strftime('%d/%m/%Y')} a {relatorios['fim_mes'].strftime('%d/%m/%Y')}"
    )


def montar_resumo_status(df_detalhe: pd.DataFrame) -> pd.DataFrame:
    if df_detalhe.empty:
        return pd.DataFrame(columns=["Status", "Composições", "Documentos", "Vencimento mais próximo"])

    ordem_status = ["VENCIDO", "VENCE HOJE", "VENCE NA SEMANA", "VENCE NO MÊS", "OK", "SEM DATA"]
    resumo = (
        df_detalhe.groupby("Status")
        .agg(
            Composições=("Composição", "nunique"),
            Documentos=("Documento", "count"),
            **{"Vencimento mais próximo": ("Vencimento", "min")},
        )
        .reset_index()
    )
    resumo["_ordem"] = resumo["Status"].map({s: i for i, s in enumerate(ordem_status)}).fillna(99)
    resumo = resumo.sort_values("_ordem").drop(columns="_ordem")
    return resumo


def preparar_visual_detalhe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    colunas = [
        "Status",
        "Composição",
        "Equipamento",
        "Placa",
        "Documento",
        "Vencimento",
        "Dias p/ vencer",
        "Origem atualização",
    ]
    return df[[c for c in colunas if c in df.columns]].copy()


st.markdown('<div class="main-title">Painel online de vencimentos por composição</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">Importe a base de composições e os Laudos KMM. O painel atualiza pela data do dia, mantém a composição em uma única linha e mostra qual documento está vencido, vence hoje ou será o próximo a vencer.</div>',
    unsafe_allow_html=True,
)

with st.container():
    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    col_imp1, col_imp2, col_imp3 = st.columns([1.1, 1.1, 0.65])
    with col_imp1:
        arquivo_base = st.file_uploader("Base controle de documentos", type=["xlsx"], key="base")
    with col_imp2:
        arquivo_kmm = st.file_uploader("Laudos KMM", type=["xlsx"], key="kmm")
    with col_imp3:
        data_ref = st.date_input("Data de referência", value=date.today(), format="DD/MM/YYYY")
    st.markdown('</div>', unsafe_allow_html=True)

if arquivo_base and arquivo_kmm:
    try:
        xls_base = pd.ExcelFile(arquivo_base)
        xls_kmm = pd.ExcelFile(arquivo_kmm)

        with st.container():
            st.markdown('<div class="section-card">', unsafe_allow_html=True)
            col_aba1, col_aba2, col_status, col_busca, col_doc, col_equip = st.columns([1, 1, 1.3, 1.15, 1.05, 1.05])
            with col_aba1:
                aba_base = st.selectbox("Aba base", xls_base.sheet_names, index=0)
            with col_aba2:
                aba_kmm = st.selectbox("Aba KMM", xls_kmm.sheet_names, index=0)
            with col_status:
                status_filtro = st.multiselect(
                    "Status",
                    ["Todos", "VENCIDO", "VENCE HOJE", "VENCE NA SEMANA", "VENCE NO MÊS", "OK"],
                    default=["VENCIDO", "VENCE HOJE", "VENCE NA SEMANA", "VENCE NO MÊS"],
                )
            with col_busca:
                placa_filtro = st.text_input("Buscar placa/composição")
            with col_doc:
                doc_filtro = st.multiselect("Documento", ["Todos", "CIV", "CIPP", "AFERIÇÃO", "CRONOTACÓGRAFO"], default=["Todos"])
            with col_equip:
                equip_filtro = st.multiselect("Equipamento", ["Todos", "Cavalo", "Carreta 1", "Carreta 2"], default=["Todos"])
            st.markdown('</div>', unsafe_allow_html=True)

        df_base = ler_base(arquivo_base, aba_base)
        df_kmm = ler_kmm(arquivo_kmm, aba_kmm)
        indice = montar_indice_kmm(df_kmm)
        relatorios = montar_relatorios(df_base, indice, data_ref)

        detalhe = relatorios["detalhe_documentos"]
        detalhe_filtrado = aplicar_filtros(detalhe, placa_filtro, doc_filtro, equip_filtro, status_filtro)
        comp_filtrada = aplicar_filtros_composicao(relatorios["base_atualizada"], detalhe_filtrado)
        comp_filtrada = dataframe_datas(comp_filtrada)
        detalhe_visual = preparar_visual_detalhe(detalhe_filtrado)
        resumo_status = montar_resumo_status(detalhe_filtrado)

        st.markdown(
            f'<div class="small-caption">{formatar_periodo(relatorios, data_ref)}</div>',
            unsafe_allow_html=True,
        )

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        with k1:
            metric_card("Composições", len(relatorios["base_atualizada"]), "total na base", "neutral")
        with k2:
            metric_card("Documentos", len(relatorios["detalhe_documentos"]), "analisados", "neutral")
        with k3:
            metric_card("Vencidos", len(relatorios["vencidos"]), "composições", "danger")
        with k4:
            metric_card("Hoje", len(relatorios["vencem_hoje"]), "vence hoje", "warning")
        with k5:
            metric_card("Semana", len(relatorios["vencem_semana"]), "próximos dias", "warning")
        with k6:
            metric_card("Mês", len(relatorios["vencem_mes"]), "após esta semana", "month")

        st.divider()

        topo1, topo2 = st.columns([0.62, 0.38])
        with topo1:
            st.subheader("Composições com documentos no filtro")
            st.caption("Visual principal: uma linha por composição, apenas placas, status e datas.")
            st.dataframe(
                comp_filtrada,
                use_container_width=True,
                hide_index=True,
                height=430,
                column_config=configurar_colunas_datas(comp_filtrada),
            )
        with topo2:
            st.subheader("Resumo do filtro")
            st.caption("Quantidade de documentos e composições por status.")
            st.dataframe(
                resumo_status,
                use_container_width=True,
                hide_index=True,
                height=248,
                column_config=configurar_colunas_datas(resumo_status),
            )

            excel_bytes = exportar_excel(relatorios, data_ref, detalhe_filtrado, comp_filtrada)
            st.download_button(
                "Baixar Excel filtrado",
                data=excel_bytes,
                file_name=f"vencimentos_documentos_{pd.Timestamp(data_ref).strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            st.info("Regra aplicada: o KMM atualiza por placa + tipo de laudo; quando existe mais de um vencimento, o sistema usa o maior vencimento como documento vigente. O status muda automaticamente conforme a data de referência: hoje aparece como VENCE HOJE, amanhã passa para VENCIDO e o painel continua mostrando o próximo documento futuro da composição.")

        st.subheader("Detalhe dos documentos encontrados")
        st.caption("Use essa tabela para saber exatamente qual documento da composição está vencido ou próximo do vencimento.")
        st.dataframe(
            detalhe_visual,
            use_container_width=True,
            hide_index=True,
            height=330,
            column_config=configurar_colunas_datas(detalhe_visual),
        )

    except Exception as erro:
        st.error(f"Não foi possível gerar o relatório: {erro}")
else:
    st.info("Envie as duas planilhas acima para liberar o painel. Depois disso, tudo aparece na mesma tela, sem abas.")
