"""
APP: Inventario en Streamlit para 2 bodegas (Crudo / Terminado) con Supabase — ADAPTADO A TU ESQUEMA REAL
Autor: ChatGPT (para Santi Correa)

Tablas existentes (según tu esquema):
- public.productos_crudos(codigo_crudo PK, detalle_crudo, cc, ff, tt, mm)
- public.relacion_crudo_terminado(codigo_terminado PK, detalle, cc, ff, tt, aa, mm, codigo_crudo FK→productos_crudos)
- public.bodega1_crudos(codigo_barras PK FK→productos_crudos.codigo_crudo, detalle, cantidad)
- public.bodega2_terminados(codigo_barras PK FK→relacion_crudo_terminado.codigo_terminado, detalle, cantidad)
- public.movimientos(id, fecha_hora, codigo_barras, movimiento ∈ {Entrada, Salida, Producción, Venta, Devolución}, cantidad>0, bodega ∈ {Bodega1, Bodega2}, usuario, observaciones)

FUNCIONALIDADES
1) Entrada a CRUDO (Bodega1)
2) Ingresar TERMINADO (Bodega2) descontando CRUDO (Bodega1) usando relacion_crudo_terminado
3) Salida de TERMINADO (Bodega2)
4) Devolución a TERMINADOS (Bodega2)
5) Corrección TERMINADO→CRUDO (descuenta B2 y suma B1)
6) Corrección CRUDO (descuento en B1)
7) Gestión de productos (crear crudo y terminado) — opcional fotos via Supabase Storage en tabla auxiliar fotos_productos
8) Dashboard de existencias y alertas

REQUISITOS
- Variables de entorno: SUPABASE_URL, SUPABASE_KEY
- (Opcional) Bucket de Storage: "productos" + tabla auxiliar "fotos_productos" para manejar URLs de imágenes

---
SQL — FUNCIONES RPC (ejecutar en Supabase SQL Editor)
-------------------------------------------------------------------
-- Asegura fila de inventario en bodega1_crudos
create or replace function ensure_b1_row(p_codigo text, p_detalle text)
returns void language plpgsql as $$
begin
  insert into bodega1_crudos(codigo_barras, detalle, cantidad)
  values (p_codigo, coalesce(p_detalle,'N/A'), 0)
  on conflict (codigo_barras) do nothing;
end;$$;

-- Asegura fila de inventario en bodega2_terminados
create or replace function ensure_b2_row(p_codigo text, p_detalle text)
returns void language plpgsql as $$
begin
  insert into bodega2_terminados(codigo_barras, detalle, cantidad)
  values (p_codigo, coalesce(p_detalle,'N/A'), 0)
  on conflict (codigo_barras) do nothing;
end;$$;

-- 1) ENTRADA CRUDO → Bodega1
create or replace function sp_entrada_crudo(
  p_codigo_crudo text,
  p_cantidad int,
  p_usuario text,
  p_obs text default 'Ingreso de crudo'
) returns void language plpgsql as $$
declare v_detalle text;
begin
  select detalle_crudo into v_detalle from productos_crudos where codigo_crudo=p_codigo_crudo;
  if v_detalle is null then raise exception 'Código crudo % no existe', p_codigo_crudo; end if;
  perform ensure_b1_row(p_codigo_crudo, v_detalle);
  update bodega1_crudos set cantidad = cantidad + p_cantidad where codigo_barras=p_codigo_crudo;
  insert into movimientos(codigo_barras, movimiento, cantidad, bodega, usuario, observaciones)
  values(p_codigo_crudo,'Entrada',p_cantidad,'Bodega1',p_usuario,p_obs);
end;$$;

-- 2) PRODUCCIÓN TERMINADO: descuenta crudo (B1) y suma terminado (B2)
create or replace function sp_producir_terminado(
  p_codigo_terminado text,
  p_cantidad int,
  p_usuario text,
  p_obs text default 'Producción / Conversión crudo→terminado'
) returns void language plpgsql as $$
declare v_codigo_crudo text; v_det_crudo text; v_det_term text; v_stock int; begin
  select codigo_crudo, detalle into v_codigo_crudo, v_det_term
  from relacion_crudo_terminado where codigo_terminado=p_codigo_terminado;
  if v_codigo_crudo is null then raise exception 'Terminado % no tiene crudo asociado', p_codigo_terminado; end if;
  select detalle_crudo into v_det_crudo from productos_crudos where codigo_crudo=v_codigo_crudo;
  if v_det_crudo is null then raise exception 'Crudo % no existe', v_codigo_crudo; end if;

  perform ensure_b1_row(v_codigo_crudo, v_det_crudo);
  perform ensure_b2_row(p_codigo_terminado, v_det_term);

  select cantidad into v_stock from bodega1_crudos where codigo_barras=v_codigo_crudo;
  if coalesce(v_stock,0) < p_cantidad then raise exception 'Stock insuficiente de crudo %', v_codigo_crudo; end if;

  update bodega1_crudos set cantidad = cantidad - p_cantidad where codigo_barras=v_codigo_crudo;
  update bodega2_terminados set cantidad = cantidad + p_cantidad where codigo_barras=p_codigo_terminado;

  -- Log movimientos (dos filas)
  insert into movimientos(codigo_barras, movimiento, cantidad, bodega, usuario, observaciones)
  values(v_codigo_crudo,'Salida',p_cantidad,'Bodega1',p_usuario,'Salida por producción');
  insert into movimientos(codigo_barras, movimiento, cantidad, bodega, usuario, observaciones)
  values(p_codigo_terminado,'Producción',p_cantidad,'Bodega2',p_usuario,p_obs);
end;$$;

-- 3) SALIDA TERMINADO (venta / retiro)
create or replace function sp_salida_terminado(
  p_codigo_terminado text,
  p_cantidad int,
  p_usuario text,
  p_obs text default 'Salida de terminado'
) returns void language plpgsql as $$
declare v_stock int; begin
  select cantidad into v_stock from bodega2_terminados where codigo_barras=p_codigo_terminado;
  if coalesce(v_stock,0) < p_cantidad then raise exception 'Stock insuficiente en Bodega2'; end if;
  update bodega2_terminados set cantidad = cantidad - p_cantidad where codigo_barras=p_codigo_terminado;
  insert into movimientos(codigo_barras, movimiento, cantidad, bodega, usuario, observaciones)
  values(p_codigo_terminado,'Salida',p_cantidad,'Bodega2',p_usuario,p_obs);
end;$$;

-- 4) DEVOLUCIÓN TERMINADO → regresa a Bodega2
create or replace function sp_devolucion_terminado(
  p_codigo_terminado text,
  p_cantidad int,
  p_usuario text,
  p_obs text default 'Devolución cliente'
) returns void language plpgsql as $$
declare v_det text; begin
  select detalle into v_det from relacion_crudo_terminado where codigo_terminado=p_codigo_terminado;
  perform ensure_b2_row(p_codigo_terminado, v_det);
  update bodega2_terminados set cantidad = cantidad + p_cantidad where codigo_barras=p_codigo_terminado;
  insert into movimientos(codigo_barras, movimiento, cantidad, bodega, usuario, observaciones)
  values(p_codigo_terminado,'Devolución',p_cantidad,'Bodega2',p_usuario,p_obs);
end;$$;

-- 5) CORRECCIÓN: TERMINADO→CRUDO (descuento B2, suma B1)
create or replace function sp_correccion_terminado_a_crudo(
  p_codigo_terminado text,
  p_cantidad int,
  p_usuario text,
  p_obs text default 'Corrección terminado→crudo'
) returns void language plpgsql as $$
declare v_codigo_crudo text; v_stock int; v_det_crudo text; v_det_term text; begin
  select codigo_crudo, detalle into v_codigo_crudo, v_det_term from relacion_crudo_terminado where codigo_terminado=p_codigo_terminado;
  if v_codigo_crudo is null then raise exception 'No hay relación crudo para %', p_codigo_terminado; end if;
  select cantidad into v_stock from bodega2_terminados where codigo_barras=p_codigo_terminado;
  if coalesce(v_stock,0) < p_cantidad then raise exception 'Stock insuficiente en Bodega2'; end if;
  select detalle_crudo into v_det_crudo from productos_crudos where codigo_crudo=v_codigo_crudo;
  perform ensure_b1_row(v_codigo_crudo, v_det_crudo);

  update bodega2_terminados set cantidad = cantidad - p_cantidad where codigo_barras=p_codigo_terminado;
  update bodega1_crudos set cantidad = cantidad + p_cantidad where codigo_barras=v_codigo_crudo;

  insert into movimientos(codigo_barras, movimiento, cantidad, bodega, usuario, observaciones)
  values(p_codigo_terminado,'Salida',p_cantidad,'Bodega2',p_usuario,p_obs);
  insert into movimientos(codigo_barras, movimiento, cantidad, bodega, usuario, observaciones)
  values(v_codigo_crudo,'Entrada',p_cantidad,'Bodega1',p_usuario,p_obs);
end;$$;

-- 6) CORRECCIÓN: CRUDO (solo descuento en B1)
create or replace function sp_correccion_crudo_descuento(
  p_codigo_crudo text,
  p_cantidad int,
  p_usuario text,
  p_obs text default 'Corrección crudo (descuento)'
) returns void language plpgsql as $$
declare v_stock int; begin
  select cantidad into v_stock from bodega1_crudos where codigo_barras=p_codigo_crudo;
  if coalesce(v_stock,0) < p_cantidad then raise exception 'Stock insuficiente en Bodega1'; end if;
  update bodega1_crudos set cantidad = cantidad - p_cantidad where codigo_barras=p_codigo_crudo;
  insert into movimientos(codigo_barras, movimiento, cantidad, bodega, usuario, observaciones)
  values(p_codigo_crudo,'Salida',p_cantidad,'Bodega1',p_usuario,p_obs);
end;$$;

-- 7) Creación de productos (opcionales)
create or replace function sp_crear_producto_crudo(
  p_codigo_crudo text,
  p_detalle_crudo text
) returns void language plpgsql as $$
begin
  insert into productos_crudos(codigo_crudo, detalle_crudo) values(p_codigo_crudo, p_detalle_crudo);
  perform ensure_b1_row(p_codigo_crudo, p_detalle_crudo);
end;$$;

create or replace function sp_crear_producto_terminado(
  p_codigo_terminado text,
  p_detalle text,
  p_codigo_crudo text
) returns void language plpgsql as $$
begin
  insert into relacion_crudo_terminado(codigo_terminado, detalle, codigo_crudo)
  values(p_codigo_terminado, p_detalle, p_codigo_crudo);
  perform ensure_b2_row(p_codigo_terminado, p_detalle);
end;$$;

-- (Opcional) fotos
create table if not exists public.fotos_productos(
  id bigserial primary key,
  codigo text not null,
  url text not null,
  tipo text check (tipo in ('crudo','terminado')),
  created_at timestamptz default now()
);
create index if not exists idx_fotos_codigo on public.fotos_productos(codigo);

-------------------------------------------------------------------

APP ERP: Inventario de 2 bodegas (Crudo / Terminado) con Supabase — Versión Avanzada (DASHBOARD PRO)
Autor: ChatGPT (para Santi Correa)

REQUISITOS
- Python libs: streamlit, supabase, pandas, plotly, python-dotenv
- Variables de entorno: SUPABASE_URL, SUPABASE_KEY
- Tablas existentes (según tu esquema real):
  - public.productos_crudos(codigo_crudo PK, detalle_crudo, cc, ff, tt, mm)
  - public.relacion_crudo_terminado(codigo_terminado PK, detalle, cc, ff, tt, aa, mm, codigo_crudo FK→productos_crudos)
  - public.bodega1_crudos(codigo_barras PK FK→productos_crudos.codigo_crudo, detalle, cantidad)
  - public.bodega2_terminados(codigo_barras PK FK→relacion_crudo_terminado.codigo_terminado, detalle, cantidad)
  - public.movimientos(id, fecha_hora, codigo_barras, movimiento ∈ {Entrada, Salida, Producción, Venta, Devolución}, cantidad>0, bodega ∈ {Bodega1, Bodega2}, usuario, observaciones)
- Funciones RPC ya provistas (recomendado para atomicidad):
  - sp_entrada_crudo, sp_producir_terminado, sp_salida_terminado,
    sp_devolucion_terminado, sp_correccion_terminado_a_crudo,
    sp_correccion_crudo_descuento, sp_crear_producto_crudo, sp_crear_producto_terminado

NOTA PRECIOS (opcional)
- Si agregas precios, crea una tabla `precios_productos(codigo text primary key, precio numeric, moneda text default 'COP', updated_at timestamptz default now())`.
- Esta app detecta automáticamente si existe y muestra valorizados; si no, los KPIs de dinero quedan ocultos.
"""

import os
from datetime import datetime, timedelta, date

import pandas as pd
import streamlit as st
import plotly.express as px
from supabase import create_client, Client
from dotenv import load_dotenv
from io import BytesIO
from datetime import date, timedelta

# ==========================
# CARGA VARIABLES DE ENTORNO
# ==========================
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("⚠️ Faltan variables de entorno SUPABASE_URL o SUPABASE_KEY. Configura tu .env o secretos del despliegue.")
    st.stop()

# ==========================
# CONFIG BÁSICA (estilo pro, paleta igual)
# ==========================
st.set_page_config(
    page_title="ERP Inventario — Crudo/Terminado",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_CSS = """
<style>
:root { --bg:#ffffff; --card:#ffffff; --muted:#f4f6f8; --border:#e6e9ee; --text:#1f2a37; --sub:#475569; --accent:#2563eb; --good:#059669; --warn:#d97706; --bad:#dc2626; }
html, body, [class*="css"] { font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
.block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 18px; box-shadow: 0 2px 12px rgba(2,6,23,0.04); }
.kpi { display:flex; align-items:center; gap:12px; }
.kpi .value { font-size: 28px; font-weight: 800; color: var(--text); }
.kpi .label { font-size: 13px; color: var(--sub); margin-top: -8px; }
.kpi .pill { font-size: 12px; padding: 2px 8px; border-radius: 999px; border:1px solid var(--border); color: var(--sub); }
hr { border: none; border-top: 1px solid var(--border); margin: 0.8rem 0 1rem 0; }
.stTabs [data-baseweb="tab-list"] { gap: 8px; }
.stTabs [data-baseweb="tab"] { background: var(--muted); border-radius: 12px; padding: 8px 12px; }
.stTabs [data-baseweb="tab"]:hover { background: #eef2ff; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 8px; border:1px solid var(--border); background:#f8fafc; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ==========================
# Tablas
# ==========================
TBL_B1 = "bodega1_crudos"
TBL_B2 = "bodega2_terminados"
TBL_MOV = "movimientos"
TBL_CRUDOS = "productos_crudos"
TBL_RELA = "relacion_crudo_terminado"
TBL_PRECIOS = "precios_productos"  # opcional

# ==========================
# Supabase Client
# ==========================
@st.cache_resource
def get_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

sb = get_client()

# ==========================
# SESSION STATE (refresh immediate)
# ==========================
if "refresh_key" not in st.session_state:
    st.session_state["refresh_key"] = 0

def bump_refresh():
    st.session_state["refresh_key"] += 1

# ==========================
# UTILIDADES / DATOS
# ==========================
@st.cache_data(ttl=60)
def table_exists(table_name: str) -> bool:
    try:
        sb.table(table_name).select("count(*)").limit(1).execute()
        return True
    except Exception:
        return False

@st.cache_data(ttl=60)
def load_df(table: str, order_by: str | None = None, refresh_key: int = 0) -> pd.DataFrame:
    q = sb.table(table).select("*")
    if order_by:
        q = q.order(order_by)
    res = q.execute()
    return pd.DataFrame(res.data) if res.data else pd.DataFrame()

# **SIN CACHÉ** para vistas operativas

def load_movimientos(fecha_desde: datetime | None = None, refresh_key: int = 0) -> pd.DataFrame:
    q = sb.table(TBL_MOV).select("*")
    if fecha_desde is not None:
        q = q.gte("fecha_hora", fecha_desde.isoformat())
    res = q.execute()
    df = pd.DataFrame(res.data) if res.data else pd.DataFrame()
    if not df.empty:
        df["fecha_hora"] = pd.to_datetime(df["fecha_hora"])
    return df


def load_inventarios(refresh_key: int = 0):
    b1 = load_df(TBL_B1, "codigo_barras", refresh_key)
    b2 = load_df(TBL_B2, "codigo_barras", refresh_key)
    return b1, b2

@st.cache_data(ttl=60)
def load_catalogs(refresh_key: int = 0):
    crudos = load_df(TBL_CRUDOS, "codigo_crudo", refresh_key)
    rela = load_df(TBL_RELA, "codigo_terminado", refresh_key)
    return crudos, rela

@st.cache_data(ttl=60)
def load_precios(refresh_key: int = 0):
    if table_exists(TBL_PRECIOS):
        return load_df(TBL_PRECIOS, refresh_key=refresh_key)
    return pd.DataFrame(columns=["codigo","precio","moneda","updated_at"])

# Consulta directa SIN caché del stock de un código

def fetch_stock_live(bodega_table: str, codigo: str) -> int:
    try:
        res = sb.table(bodega_table).select("cantidad").eq("codigo_barras", codigo).single().execute()
        if res.data and "cantidad" in res.data:
            return int(res.data["cantidad"]) or 0
    except Exception:
        try:
            res = sb.table(bodega_table).select("cantidad").eq("codigo_barras", codigo).execute()
            if res.data:
                return int(res.data[0]["cantidad"]) or 0
        except Exception:
            return 0
    return 0

# RPC helper

def rpc(name: str, params: dict):
    return sb.rpc(name, params).execute()

# Rerun seguro

def safe_rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# ==========================
# Analytics helpers (KPIs avanzados)
# ==========================

def compute_totales(b1: pd.DataFrame, b2: pd.DataFrame):
    t_b1 = int(b1["cantidad"].sum()) if not b1.empty else 0
    t_b2 = int(b2["cantidad"].sum()) if not b2.empty else 0
    t_all = t_b1 + t_b2
    p_b1 = (t_b1 / t_all * 100) if t_all else 0
    p_b2 = (t_b2 / t_all * 100) if t_all else 0
    skus_b1 = b1.shape[0]; skus_b2 = b2.shape[0]
    return t_b1, t_b2, t_all, p_b1, p_b2, skus_b1, skus_b2


def join_precios(inv_df: pd.DataFrame, precios: pd.DataFrame) -> pd.DataFrame:
    if inv_df.empty:
        inv_df = pd.DataFrame(columns=["codigo","cantidad","valor","precio"])
        return inv_df
    if "codigo" not in inv_df.columns:
        inv_df = inv_df.rename(columns={"codigo_barras":"codigo"})
    out = inv_df.merge(precios[["codigo","precio"]], on="codigo", how="left")
    out["precio"].fillna(0, inplace=True)
    out["valor"] = out["cantidad"] * out["precio"]
    return out


def compute_rotacion_y_cobertura(mov: pd.DataFrame, ventana_dias=30):
    """Rotación: unidades salidas/Venta en B2 en ventana.
       Cobertura: días de stock (inventario_total / promedio diario de salidas).
    """
    if mov.empty:
        return pd.DataFrame(columns=["codigo_barras","rotacion_30d","avg_diario","cobertura_dias"])  
    desde = pd.Timestamp.utcnow() - pd.Timedelta(days=ventana_dias)
    d = mov[(mov["fecha_hora"] >= desde) & (mov["bodega"]=="Bodega2") & (mov["movimiento"].isin(["Salida","Venta"]))]
    rot = d.groupby("codigo_barras")["cantidad"].sum().reset_index().rename(columns={"cantidad":"rotacion_30d"})
    rot["avg_diario"] = rot["rotacion_30d"] / ventana_dias
    return rot


def evolucion_inventario(mov: pd.DataFrame, dias=60) -> pd.DataFrame:
    if mov.empty:
        return pd.DataFrame(columns=["fecha","Bodega1","Bodega2"])
    desde = pd.Timestamp.utcnow() - pd.Timedelta(days=dias)
    df = mov[mov["fecha_hora"] >= desde].copy()
    df["fecha"] = df["fecha_hora"].dt.date
    df["signo"] = df["movimiento"].map({"Entrada":1,"Devolución":1,"Producción":1,"Salida":-1,"Venta":-1}).fillna(0)
    df["ajuste"] = df["cantidad"] * df["signo"]
    agg = df.groupby(["fecha","bodega"]).agg(total=("ajuste","sum")).reset_index()
    pivot = agg.pivot(index="fecha", columns="bodega", values="total").fillna(0).reset_index()
    pivot["Bodega1"] = pivot.get("Bodega1", 0).cumsum()
    pivot["Bodega2"] = pivot.get("Bodega2", 0).cumsum()
    return pivot

# ==========================
# SIDEBAR
# ==========================
with st.sidebar:
    st.markdown("### ⚙️ ERP — Navegación")
    main_section = st.radio("Sección", ["📊 Dashboard","🧰 Gestión de Inventario"], index=0)
    st.markdown("---")
    usuario = st.text_input("Usuario", value="system")
    st.caption("Se usa para registrar movimientos.")
    st.markdown("---")
    st.markdown("#### 🎯 Filtros del Dashboard")
    hoy = date.today()
    rango = st.select_slider("Rango de análisis", options=[7,14,30,60,90], value=30, help="Ventana para KPIs de rotación y evolución")
    umbral = st.number_input("Umbral stock crítico", min_value=0, value=5)
    ver_bodega = st.multiselect("Bodegas a mostrar", ["Bodega1","Bodega2"], default=["Bodega1","Bodega2"])    
    st.markdown("---")
    if st.button("🔄 Refrescar todo"):
        bump_refresh(); safe_rerun()

# ==========================
# SECCIÓN: DASHBOARD (PRO)
# ==========================
if main_section == "📊 Dashboard":
    st.markdown("# 📊 Dashboard de Inventario (Poliartes)")

    crudos, rela = load_catalogs(st.session_state["refresh_key"])
    b1, b2 = load_inventarios(st.session_state["refresh_key"])
    mov = load_movimientos(refresh_key=st.session_state["refresh_key"]) 
    precios = load_precios(st.session_state["refresh_key"])

    # KPIs base
    t_b1, t_b2, t_all, p_b1, p_b2, skus_b1, skus_b2 = compute_totales(b1, b2)
    # ====== Exportar Excel: Bodega 2 (Inventario + Movimientos) ======
    st.markdown("### ⬇️ Exportar Excel — Bodega 2")
    
    # Filtros de fecha para movimientos (por defecto últimos 30 días)
    col_exp1, col_exp2, col_exp3 = st.columns([1,1,1])
    with col_exp1:
        fecha_desde = st.date_input("Desde", value=date.today() - timedelta(days=30))
    with col_exp2:
        fecha_hasta = st.date_input("Hasta", value=date.today(), min_value=fecha_desde)
    with col_exp3:
        st.write("")  # espaciador
    
    # Preparar DF de INVENTARIO actual Bodega2
    inv_b2_xls = b2[["codigo_barras", "detalle", "cantidad"]].copy().sort_values("codigo_barras")
    
    # Preparar DF de MOVIMIENTOS Bodega2 (ingresos/salidas con fecha)
    mov_b2 = mov.copy()
    if not mov_b2.empty:
        mov_b2 = mov_b2[
            (mov_b2["bodega"] == "Bodega2") &
            (mov_b2["movimiento"].isin(["Producción", "Devolución", "Salida", "Venta"])) &
            (mov_b2["fecha_hora"].dt.date >= fecha_desde) &
            (mov_b2["fecha_hora"].dt.date <= fecha_hasta)
        ].copy()
    
        # Añadir detalle de producto al movimiento (catálogo de terminados)
        if not rela.empty:
            det_term = rela.rename(columns={"codigo_terminado": "codigo_barras", "detalle": "detalle_terminado"})[
                ["codigo_barras", "detalle_terminado"]
            ]
            mov_b2 = mov_b2.merge(det_term, on="codigo_barras", how="left")
    
        # Ordenar y seleccionar columnas
        mov_b2 = mov_b2.sort_values("fecha_hora")[
            ["fecha_hora", "codigo_barras", "detalle_terminado", "movimiento", "cantidad", "usuario", "observaciones"]
        ].rename(columns={
            "fecha_hora": "fecha",
            "detalle_terminado": "detalle"
        })
    else:
        mov_b2 = pd.DataFrame(columns=["fecha","codigo_barras","detalle","movimiento","cantidad","usuario","observaciones"])
    
    # Botón de descarga (Excel en memoria)
    buffer = BytesIO()
    if st.button("Generar Excel de Bodega 2"):
        with pd.ExcelWriter(buffer, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm:ss") as writer:
            # Sheet 1: Inventario actual
            inv_b2_xls.to_excel(writer, index=False, sheet_name="Inventario_B2")
            ws1 = writer.sheets["Inventario_B2"]
    
            # Sheet 2: Movimientos (ingresos/salidas)
            mov_b2.to_excel(writer, index=False, sheet_name="Movimientos_B2")
            ws2 = writer.sheets["Movimientos_B2"]
    
            # Autoajuste de columnas (simple)
            def autosize(ws, df):
                for idx, col in enumerate(df.columns):
                    try:
                        max_len = max(
                            [len(str(col))] + [len(str(x)) for x in df[col].astype(str).values]
                        )
                    except Exception:
                        max_len = len(str(col))
                    ws.set_column(idx, idx, min(max_len + 2, 40))  # ancho máx 40
            autosize(ws1, inv_b2_xls)
            autosize(ws2, mov_b2)
    
        buffer.seek(0)
        st.download_button(
            label="⬇️ Descargar Excel",
            data=buffer.getvalue(),
            file_name=f"Bodega2_{fecha_desde.isoformat()}_{fecha_hasta.isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    # Valor inventario
    inv_b1 = b1.rename(columns={"codigo_barras":"codigo"})
    inv_b2 = b2.rename(columns={"codigo_barras":"codigo"})
    val_b1 = join_precios(inv_b1, precios)["valor"].sum()
    val_b2 = join_precios(inv_b2, precios)["valor"].sum()

    # Rotación & cobertura (30/60/90 según slider)
    rot = compute_rotacion_y_cobertura(mov, ventana_dias=rango)

    # Cobertura general (B2): inventario total B2 / avg diario salidas B2
    b2_tot = t_b2
    avg_diario_b2 = rot["avg_diario"].sum() if not rot.empty else 0
    cobertura_dias_b2 = (b2_tot / avg_diario_b2) if avg_diario_b2 else None

    # Críticos
    crit_b1 = b1[b1["cantidad"] <= umbral]
    crit_b2 = b2[b2["cantidad"] <= umbral]

    # KPIs Cards
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f"<div class='card kpi'><div><div class='label'>Bodega1 (Crudos)</div><div class='value'>{t_b1}</div><div class='pill'>{p_b1:.1f}% · {skus_b1} SKUs</div></div></div>", unsafe_allow_html=True)
    with c2:
        st.markdown(f"<div class='card kpi'><div><div class='label'>Bodega2 (Terminados)</div><div class='value'>{t_b2}</div><div class='pill'>{p_b2:.1f}% · {skus_b2} SKUs</div></div></div>", unsafe_allow_html=True)
    with c3:
        if not precios.empty:
            st.markdown("<div class='card kpi'><div><div class='label'>Valor Inventario</div><div class='value'>${:,.0f}</div><div class='pill'>B1 ${:,.0f} · B2 ${:,.0f}</div></div></div>".format(val_b1+val_b2, val_b1, val_b2), unsafe_allow_html=True)
        else:
            st.markdown("<div class='card kpi'><div><div class='label'>Valor Inventario</div><div class='value'>N/D</div><div class='pill'>Agrega precios_productos</div></div></div>", unsafe_allow_html=True)
    with c4:
        cov_txt = f"{cobertura_dias_b2:.1f} días" if cobertura_dias_b2 is not None else "N/D"
        st.markdown(f"<div class='card kpi'><div><div class='label'>Cobertura (B2)</div><div class='value'>{cov_txt}</div><div class='pill'>Ventana {rango}d</div></div></div>", unsafe_allow_html=True)

    # Fila de gráficos: composición y evolución
    g1, g2 = st.columns(2)
    with g1:
        st.markdown("### 🥧 Composición por Bodega")
        comp_df = pd.DataFrame({"Bodega":["Bodega1","Bodega2"], "Unidades":[t_b1, t_b2]})
        fig_pie = px.pie(comp_df, names="Bodega", values="Unidades", hole=0.45)
        st.plotly_chart(fig_pie, use_container_width=True)
    with g2:
        st.markdown("### 📈 Evolución (últimos {} días)".format(rango))
        evo = evolucion_inventario(mov, dias=rango)
        if not evo.empty:
            evo_long = evo.melt(id_vars=["fecha"], value_vars=["Bodega1","Bodega2"], var_name="Bodega", value_name="Unidades Acum")
            fig2 = px.line(evo_long, x="fecha", y="Unidades Acum", color="Bodega", markers=True)
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("Sin datos suficientes para evolución.")

    st.markdown("---")

    # Top rotación & críticos
    g3, g4 = st.columns(2)
    with g3:
        # Top 5 Rotación (rango días)
        st.markdown(f"### 🔝 Top 5 Rotación ({rango} días)")
        if not rot.empty:
            top = rot.sort_values("rotacion_30d", ascending=False).head(5)

            # Construir mapa de detalles SIN funciones raras en el concat
            det_parts = []
            if not rela.empty:
                det_term = rela.rename(columns={"codigo_terminado": "codigo_barras", "detalle": "detalle"})[
                    ["codigo_barras", "detalle"]
                ]
                det_parts.append(det_term)
            if not crudos.empty:
                det_cru = crudos.rename(columns={"codigo_crudo": "codigo_barras", "detalle_crudo": "detalle"})[
                    ["codigo_barras", "detalle"]
                ]
                det_parts.append(det_cru)

            det_map = (
                pd.concat(det_parts, ignore_index=True)
                if det_parts
                else pd.DataFrame(columns=["codigo_barras", "detalle"])
            )

            # Unir detalles al top
            top = top.merge(det_map, on="codigo_barras", how="left")

            # Gráfico y tabla
            fig_top = px.bar(top, x="rotacion_30d", y="detalle", orientation="h", text="rotacion_30d")
            fig_top.update_layout(yaxis_title="Producto", xaxis_title="Unidades")
            st.plotly_chart(fig_top, use_container_width=True)

            st.dataframe(
                top.rename(columns={"codigo_barras": "Código", "rotacion_30d": "Rotación"})[
                    ["Código", "detalle", "Rotación"]
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No hay movimientos suficientes para rotación.")


    with g4:
        st.markdown("### ⚠️ Críticos (<= umbral)")
        if not crit_b1.empty or not crit_b2.empty:
            crit_b1_v = crit_b1.assign(bodega="Bodega1")[ ["bodega","codigo_barras","detalle","cantidad"] ]
            crit_b2_v = crit_b2.assign(bodega="Bodega2")[ ["bodega","codigo_barras","detalle","cantidad"] ]
            crit_all = pd.concat([crit_b1_v, crit_b2_v], ignore_index=True)
            st.dataframe(crit_all.sort_values(["bodega","cantidad"]), use_container_width=True, hide_index=True)
            csv = crit_all.to_csv(index=False).encode('utf-8')
            st.download_button("⬇️ Descargar críticos (CSV)", data=csv, file_name="criticos.csv", mime="text/csv")
        else:
            st.success("Sin críticos. 🎉")

    # Inventarios por bodega con búsqueda
    st.markdown("---")
    st.markdown("### 📦 Inventarios por Bodega (con búsqueda)")
    q = st.text_input("Buscar por código o detalle")
    if "Bodega1" in ver_bodega:
        df1 = b1.copy()
        if q:
            ql = q.lower()
            df1 = df1[df1.apply(lambda r: ql in str(r["codigo_barras"]).lower() or ql in str(r["detalle"]).lower(), axis=1)]
        st.markdown("**Bodega1 — Crudos**")
        st.dataframe(df1[["codigo_barras","detalle","cantidad"]].sort_values("codigo_barras"), use_container_width=True, hide_index=True)
    if "Bodega2" in ver_bodega:
        df2 = b2.copy()
        if q:
            ql = q.lower()
            df2 = df2[df2.apply(lambda r: ql in str(r["codigo_barras"]).lower() or ql in str(r["detalle"]).lower(), axis=1)]
        st.markdown("**Bodega2 — Terminados**")
        st.dataframe(df2[["codigo_barras","detalle","cantidad"]].sort_values("codigo_barras"), use_container_width=True, hide_index=True)

# ==========================
# SECCIÓN: GESTIÓN DE INVENTARIO (sin cambios funcionales)
# ==========================
else:
    st.markdown("# 🧰 Gestión de Inventario")
    tabs = st.tabs([
        "Entrada Crudo",
        "Producción / Conversión",
        "Salida Terminado",
        "Devolución Terminado",
        "Correcciones",
        "Productos",
    ])

    crudos, rela = load_catalogs(st.session_state["refresh_key"])
    b1, b2 = load_inventarios(st.session_state["refresh_key"])

    # Maps
    map_crudo = {f"{r['codigo_crudo']} — {r.get('detalle_crudo','')}": r['codigo_crudo'] for _, r in crudos.iterrows()} if not crudos.empty else {}
    map_term = {f"{r['codigo_terminado']} — {r.get('detalle','')}": r['codigo_terminado'] for _, r in rela.iterrows()} if not rela.empty else {}

    # -------------------------
    # Entrada Crudo
    # -------------------------
    with tabs[0]:
        st.markdown("### ➕ Entrada a Bodega1 (CRUDO)")
        if not map_crudo:
            st.warning("No hay productos crudos. Crea uno en la pestaña Productos.")
        else:
            col = st.columns([2,1,2])
            with col[0]:
                sel = st.selectbox("Producto crudo", list(map_crudo.keys()))
            with col[1]:
                cant = st.number_input("Cantidad", min_value=1, step=1)
            with col[2]:
                obs = st.text_input("Observaciones", "Ingreso de crudo")

            codigo = map_crudo.get(sel)
            stock_act = fetch_stock_live(TBL_B1, codigo)
            st.markdown(f"**Stock actual Bodega1:** {stock_act} und")

            if st.button("Registrar entrada", key="btn_ent_crudo"):
                try:
                    with st.spinner("Registrando entrada..."):
                        rpc("sp_entrada_crudo", {"p_codigo_crudo": codigo, "p_cantidad": int(cant), "p_usuario": usuario, "p_obs": obs})
                    st.success(f"Entrada registrada ✅ · Stock ahora: {fetch_stock_live(TBL_B1, codigo)}")
                    bump_refresh(); safe_rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

            st.markdown("#### Inventario del producto (Bodega1)")
            st.dataframe(load_inventarios(st.session_state["refresh_key"])[0].query("codigo_barras == @codigo"), use_container_width=True, hide_index=True)

    # -------------------------
    # Producción / Conversión
    # -------------------------
    with tabs[1]:
        st.markdown("### ✅ Producir TERMINADO descontando CRUDO")
        if not map_term:
            st.warning("No hay productos terminados. Crea uno en la pestaña Productos.")
        else:
            col = st.columns([2,1,2])
            with col[0]:
                sel_t = st.selectbox("Producto terminado (destino)", list(map_term.keys()))
            with col[1]:
                cant = st.number_input("Cantidad a producir", min_value=1, step=1, key="cant_prod")
            with col[2]:
                obs = st.text_input("Observaciones", "Producción / Conversión", key="obs_prod")

            cod_t = map_term.get(sel_t)
            cod_crudo_row = rela[rela["codigo_terminado"]==cod_t]
            cod_c = cod_crudo_row["codigo_crudo"].iloc[0] if not cod_crudo_row.empty else None

            stock_c = fetch_stock_live(TBL_B1, cod_c) if cod_c else 0
            stock_t = fetch_stock_live(TBL_B2, cod_t)
            st.markdown(f"**Stock CRUDO (B1):** {stock_c} · **Stock TERMINADO (B2):** {stock_t}")

            if st.button("Producir e ingresar", key="btn_prod"):
                try:
                    with st.spinner("Procesando producción..."):
                        rpc("sp_producir_terminado", {"p_codigo_terminado": cod_t, "p_cantidad": int(cant), "p_usuario": usuario, "p_obs": obs})
                    st.success("Producción registrada ✅")
                    st.info(f"CRUDO (B1) ahora: {fetch_stock_live(TBL_B1, cod_c)} · TERMINADO (B2) ahora: {fetch_stock_live(TBL_B2, cod_t)}")
                    bump_refresh(); safe_rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

            st.markdown("#### Inventario del producto")
            cA, cB = st.columns(2)
            with cA:
                st.markdown("**Bodega1 (Crudo base)**")
                st.dataframe(load_inventarios(st.session_state["refresh_key"])[0].query("codigo_barras == @cod_c"), use_container_width=True, hide_index=True)
            with cB:
                st.markdown("**Bodega2 (Terminado)**")
                st.dataframe(load_inventarios(st.session_state["refresh_key"])[1].query("codigo_barras == @cod_t"), use_container_width=True, hide_index=True)

    # -------------------------
    # Salida Terminado
    # -------------------------
    with tabs[2]:
        st.markdown("### 📦 Salida de Terminados (Bodega2)")
        if not map_term:
            st.warning("No hay productos terminados.")
        else:
            col = st.columns([2,1,2])
            with col[0]:
                sel_t = st.selectbox("Producto terminado", list(map_term.keys()), key="sel_sal")
            with col[1]:
                cant = st.number_input("Cantidad a sacar", min_value=1, step=1, key="cant_sal")
            with col[2]:
                obs = st.text_input("Observaciones", "Venta / Retiro", key="obs_sal")

            cod_t = map_term.get(sel_t)
            stock_t = fetch_stock_live(TBL_B2, cod_t)
            st.markdown(f"**Stock TERMINADO (B2):** {stock_t}")

            if st.button("Registrar salida", key="btn_sal"):
                try:
                    with st.spinner("Registrando salida..."):
                        rpc("sp_salida_terminado", {"p_codigo_terminado": cod_t, "p_cantidad": int(cant), "p_usuario": usuario, "p_obs": obs})
                    st.success(f"Salida registrada ✅ · Stock B2 ahora: {fetch_stock_live(TBL_B2, cod_t)}")
                    bump_refresh(); safe_rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

            st.markdown("#### Inventario del producto (Bodega2)")
            st.dataframe(load_inventarios(st.session_state["refresh_key"])[1].query("codigo_barras == @cod_t"), use_container_width=True, hide_index=True)

    # -------------------------
    # Devolución Terminado
    # -------------------------
    with tabs[3]:
        st.markdown("### ♻️ Devolución a Terminados (Bodega2)")
        if not map_term:
            st.warning("No hay productos terminados.")
        else:
            col = st.columns([2,1,2])
            with col[0]:
                sel_t = st.selectbox("Producto devuelto (terminado)", list(map_term.keys()), key="sel_dev")
            with col[1]:
                cant = st.number_input("Cantidad devuelta", min_value=1, step=1, key="cant_dev")
            with col[2]:
                obs = st.text_input("Observaciones", "Devolución cliente / Corrección", key="obs_dev")

            cod_t = map_term.get(sel_t)
            stock_t = fetch_stock_live(TBL_B2, cod_t)
            st.markdown(f"**Stock TERMINADO (B2):** {stock_t}")

            if st.button("Registrar devolución", key="btn_dev"):
                try:
                    with st.spinner("Registrando devolución..."):
                        rpc("sp_devolucion_terminado", {"p_codigo_terminado": cod_t, "p_cantidad": int(cant), "p_usuario": usuario, "p_obs": obs})
                    st.success(f"Devolución registrada ✅ · Stock B2 ahora: {fetch_stock_live(TBL_B2, cod_t)}")
                    bump_refresh(); safe_rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

            st.markdown("#### Inventario del producto (Bodega2)")
            st.dataframe(load_inventarios(st.session_state["refresh_key"])[1].query("codigo_barras == @cod_t"), use_container_width=True, hide_index=True)

    # -------------------------
    # Correcciones
    # -------------------------
    with tabs[4]:
        sub1, sub2 = st.tabs(["Terminado → Crudo","Crudo (descuento)"])

        with sub1:
            st.markdown("#### 🛠️ Corrección: descontar TERMINADO y regresar a CRUDO")
            if not map_term:
                st.warning("No hay productos terminados.")
            else:
                col = st.columns([2,1,2])
                with col[0]:
                    sel_t = st.selectbox("Producto TERMINADO", list(map_term.keys()), key="sel_cor_t")
                with col[1]:
                    cant = st.number_input("Cantidad a corregir", min_value=1, step=1, key="cant_cor_t")
                with col[2]:
                    obs = st.text_input("Observaciones", "Corrección / Reproceso", key="obs_cor_t")

                cod_t = map_term.get(sel_t)
                row = rela[rela["codigo_terminado"]==cod_t]
                cod_c = row["codigo_crudo"].iloc[0] if not row.empty else None
                stock_t = fetch_stock_live(TBL_B2, cod_t)
                stock_c = fetch_stock_live(TBL_B1, cod_c) if cod_c else 0
                st.markdown(f"**Stock TERMINADO (B2):** {stock_t} · **Stock CRUDO (B1):** {stock_c}")

                if st.button("Aplicar corrección", key="btn_cor_t"):
                    try:
                        with st.spinner("Aplicando corrección..."):
                            rpc("sp_correccion_terminado_a_crudo", {"p_codigo_terminado": cod_t, "p_cantidad": int(cant), "p_usuario": usuario, "p_obs": obs})
                        st.success("Corrección aplicada ✅")
                        st.info(f"B2 ahora: {fetch_stock_live(TBL_B2, cod_t)} · B1 ahora: {fetch_stock_live(TBL_B1, cod_c)}")
                        bump_refresh(); safe_rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

                cA, cB = st.columns(2)
                with cA:
                    st.markdown("**Bodega2 (Terminado)**")
                    st.dataframe(load_inventarios(st.session_state["refresh_key"])[1].query("codigo_barras == @cod_t"), use_container_width=True, hide_index=True)
                with cB:
                    st.markdown("**Bodega1 (Crudo devuelto)**")
                    st.dataframe(load_inventarios(st.session_state["refresh_key"])[0].query("codigo_barras == @cod_c"), use_container_width=True, hide_index=True)

        with sub2:
            st.markdown("#### 🛠️ Corrección: solo descuento en CRUDO (Bodega1)")
            if not map_crudo:
                st.warning("No hay productos crudos.")
            else:
                col = st.columns([2,1,2])
                with col[0]:
                    sel_c = st.selectbox("Producto CRUDO", list(map_crudo.keys()), key="sel_cor_c")
                with col[1]:
                    cant = st.number_input("Cantidad a descontar", min_value=1, step=1, key="cant_cor_c")
                with col[2]:
                    obs = st.text_input("Observaciones", "Ajuste inventario / Merma", key="obs_cor_c")

                cod_c = map_crudo.get(sel_c)
                stock_c = fetch_stock_live(TBL_B1, cod_c)
                st.markdown(f"**Stock CRUDO (B1):** {stock_c}")

                if st.button("Aplicar descuento", key="btn_cor_c"):
                    try:
                        with st.spinner("Aplicando corrección..."):
                            rpc("sp_correccion_crudo_descuento", {"p_codigo_crudo": cod_c, "p_cantidad": int(cant), "p_usuario": usuario, "p_obs": obs})
                        st.success(f"Corrección aplicada ✅ · B1 ahora: {fetch_stock_live(TBL_B1, cod_c)}")
                        bump_refresh(); safe_rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

                st.markdown("**Inventario (Bodega1)")
                st.dataframe(load_inventarios(st.session_state["refresh_key"])[0].query("codigo_barras == @cod_c"), use_container_width=True, hide_index=True)

    # -------------------------
    # Productos
    # -------------------------
    with tabs[5]:
        st.markdown("### 🧩 Gestión de Productos")
        cA, cB = st.columns(2)

        with cA:
            st.subheader("➕ Crear CRUDO")
            codigo_c = st.text_input("Código crudo")
            detalle_c = st.text_input("Detalle crudo")
            if st.button("Crear CRUDO", key="btn_new_c"):
                if not codigo_c:
                    st.error("Código requerido")
                else:
                    try:
                        with st.spinner("Creando crudo..."):
                            rpc("sp_crear_producto_crudo", {"p_codigo_crudo": codigo_c, "p_detalle_crudo": detalle_c})
                        st.success("CRUDO creado ✅")
                        bump_refresh(); safe_rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

        with cB:
            st.subheader("➕ Crear TERMINADO")
            codigo_t = st.text_input("Código terminado")
            detalle_t = st.text_input("Detalle terminado")
            base_crudo = st.selectbox("Crudo base (relación)", list(map_crudo.keys())) if map_crudo else st.text_input("Código crudo base")
            if st.button("Crear TERMINADO", key="btn_new_t"):
                cod_base = map_crudo.get(base_crudo, base_crudo)
                if not codigo_t or not cod_base:
                    st.error("Código terminado y crudo base son requeridos")
                else:
                    try:
                        with st.spinner("Creando terminado..."):
                            rpc("sp_crear_producto_terminado", {"p_codigo_terminado": codigo_t, "p_detalle": detalle_t, "p_codigo_crudo": cod_base})
                        st.success("TERMINADO creado ✅")
                        bump_refresh(); safe_rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Catálogo CRUDO**")
            st.dataframe(crudos, use_container_width=True, hide_index=True)
        with c2:
            st.markdown("**Catálogo TERMINADO (relación)**")
            st.dataframe(rela, use_container_width=True, hide_index=True)

# ==========================
# FOOTER
# ==========================
st.markdown("""
---
**Notas**
- App demo para Poliartes (2024) con Streamlit + Supabase.
- Dashboard PRO: KPIs ampliados (SKUs, valor, cobertura), composición, evolución, top rotación, críticos, búsqueda y filtros por ventana/umbral/bodegas.
- Gestión de inventario: sin cambios funcionales, con refresh inmediato.
- Si creas `precios_productos`, aparecerán KPIs de valorizado automáticamente.

""")

