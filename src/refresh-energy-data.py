import urllib.request
import urllib.parse
import json
import socket
import os
import re
import threading
import time
import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from datetime import datetime, timedelta

# --- CONFIGURAÇÕES ---
PORT = 8080
SHELLY_EM_IP = "192.168.68.105"  # SUBSTITUA PELO IP REAL DO SEU SHELLY EM
TARIFA_ACESSO = 0.06         # Fora de Vazio em Bi-horário (Mude se necessário)
FADEQU = 1.02
PERDAS_E_TAXAS = 0.0155
IVA = 1.23
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR) # Pasta TDI onde está o index.html
FICHEIRO_PRECOS_NAME = "precos_omie.txt"
FICHEIRO_PRECOS = os.path.join(BASE_DIR, FICHEIRO_PRECOS_NAME)  # Ficheiro local antigo/alternativo
CACHE_FOLDER = os.path.join(BASE_DIR, "cache_omie")
META_FILE = os.path.join(BASE_DIR, "precos_omie.meta")
SHELLY_CONSUMPTION_STATE = os.path.join(BASE_DIR, "shelly_consumo.json")
OMIE_LIST_URL = "https://www.omie.es/pt/file-access-list?parents=/%20Mercado%20Di%C3%A1rio/1.%20Pre%C3%A7os&dir=%20Pre%C3%A7os%20por%20hora%20do%20mercado%20di%C3%A1rio%20em%20Portugal&realdir=marginalpdbcpt"
OMIE_DOWNLOAD_BASE = "https://www.omie.es/pt/file-download?parents=marginalpdbcpt&filename="
CACHE_CHECK_INTERVAL = 3600  # segundos entre verificações de atualização
HORAS_PREVISAO = 6  # Quantas horas para mostrar no semáforo futuro
INTERVALO_PREVISAO_MIN = 15  # minutos por bloco na previsão
RETENCAO_CACHE = 3  # quantos ficheiros OMIE manter localmente
# ---------------------

# Configuração de Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Semaforo Energia API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def obter_ip_local():
    """Obtém o IP local do PC"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"

def garantir_pasta_cache():
    try:
        os.makedirs(CACHE_FOLDER, exist_ok=True)
    except Exception as e:
        logger.error(f"Nao foi possivel criar pasta de cache: {e}")


def caminho_cache(nome_ficheiro):
    return os.path.join(CACHE_FOLDER, nome_ficheiro)


def listar_ficheiros_cache():
    if not os.path.isdir(CACHE_FOLDER):
        return []
    nomes = [f for f in os.listdir(CACHE_FOLDER) if re.match(r'marginalpdbcpt_\d{8}\.1$', f)]
    nomes.sort(reverse=True)
    return nomes


def parse_precos_omie_local():
    garantir_pasta_cache()
    entradas = []

    arquivos = listar_ficheiros_cache()
    if os.path.exists(FICHEIRO_PRECOS):
        arquivos.insert(0, FICHEIRO_PRECOS)

    logger.info(f"Ficheiros locais OMIE: {arquivos}")

    for nome in arquivos:
        caminho = caminho_cache(nome) if nome != FICHEIRO_PRECOS else nome
        try:
            with open(caminho, 'r', encoding='utf-8') as f:
                linhas = f.readlines()
        except Exception as e:
            logger.error(f"Nao foi possivel ler {caminho}: {e}")
            continue

        for linha in linhas:
            partes = linha.strip().split(';')
            if len(partes) < 5 or not partes[0].isdigit():
                continue
            try:
                ano = int(partes[0])
                mes = int(partes[1])
                dia = int(partes[2])
                hora = int(partes[3])
                preco = None
                for valor in reversed(partes[4:]):
                    if valor.strip() == '':
                        continue
                    preco = float(valor)
                    break
                if preco is None:
                    continue
            except Exception:
                continue

            dt = datetime(ano, mes, dia, 0, 0) + timedelta(hours=hora - 1)
            entradas.append((dt, preco))

    entradas.sort(key=lambda x: x[0])
    print(f"Entradas localizadas: {len(entradas)}")
    return entradas


def obter_preco_omie_local():
    entradas = parse_precos_omie_local()
    if not entradas:
        return None

    agora = datetime.now().replace(minute=0, second=0, microsecond=0)
    for dt, preco in entradas:
        if dt == agora:
            logger.info(f"Preco OMIE local: {preco} €/MWh ({dt.strftime('%H:%M')})")
            return preco

    fallback = None
    for dt, preco in reversed(entradas):
        if dt <= agora:
            fallback = (dt, preco)
            break
    if fallback:
        dt, preco = fallback
        logger.warning(f"Hora exata não encontrada. Usando fallback {dt.strftime('%H:%M')}: {preco} €/MWh")
        return preco

    dt, preco = entradas[0]
    logger.warning(f"Usando primeiro preço disponível: {preco} €/MWh")
    return preco


def obter_previsao_omie_local(horas=HORAS_PREVISAO, intervalo_minutos=INTERVALO_PREVISAO_MIN):
    entradas = parse_precos_omie_local()
    if not entradas:
        return []

    agora = datetime.now().replace(minute=0, second=0, microsecond=0)
    preco_por_hora = {dt.replace(minute=0, second=0, microsecond=0): preco for dt, preco in entradas}

    previsao = []
    total_blocos = int(horas * 60 / intervalo_minutos)
    for i in range(total_blocos):
        bloco_dt = agora + timedelta(minutes=i * intervalo_minutos)
        bloco_hora = bloco_dt.replace(minute=0, second=0, microsecond=0)
        preco_mwh = preco_por_hora.get(bloco_hora)
        if preco_mwh is None:
            # tenta encontrar o último preço conhecido antes do bloco
            possivel = [preco for dt, preco in entradas if dt <= bloco_hora]
            if possivel:
                preco_mwh = possivel[-1]
        if preco_mwh is None:
            continue

        preco_kwh_base = preco_mwh / 1000
        preco_final_kwh = ((preco_kwh_base * FADEQU * 1.15) + PERDAS_E_TAXAS + TARIFA_ACESSO) * IVA
        cor, mensagem = obter_semaforo_por_preco(preco_final_kwh)
        previsao.append({
            "hora": bloco_dt.strftime('%d/%m %H:%M'),
            "preco_kwh": round(preco_final_kwh, 3),
            "cor": cor,
            "motivo": mensagem,
            "valor": preco_final_kwh
        })

    return previsao


def obter_semaforo_por_preco(preco_final_kwh):
    if preco_final_kwh <= 0.12:
        return "VERDE", f"Rede barata ({preco_final_kwh:.3f}€/kWh)"
    elif preco_final_kwh < 0.22:
        return "AMARELO", f"Preço moderado ({preco_final_kwh:.3f}€/kWh)"
    else:
        return "VERMELHO", f"Pico de preço ({preco_final_kwh:.3f}€/kWh)"


def ler_nome_meta_local():
    try:
        if not os.path.exists(META_FILE):
            return None
        with open(META_FILE, 'r', encoding='utf-8') as f:
            nome = f.read().strip()
        return nome or None
    except Exception as e:
        logger.error(f"Erro ao ler meta local: {e}")
        return None


def gravar_nome_meta_local(nome):
    try:
        with open(META_FILE, 'w', encoding='utf-8') as f:
            f.write(nome)
    except Exception as e:
        logger.error(f"Erro ao gravar meta local: {e}")


def obter_ultima_fonte_omie():
    try:
        req = urllib.request.Request(OMIE_LIST_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"Erro ao consultar lista OMIE: {e}")
        return None

    html = html.replace('&amp;', '&')
    pattern = re.compile(r'href=["\'](/pt/file-download\?parents=marginalpdbcpt&filename=(marginalpdbcpt_\d{8}\.1))["\']', re.IGNORECASE)
    matches = pattern.findall(html)
    if not matches:
        logger.error("Não foi possível encontrar ficheiros na lista OMIE")
        return None

    nomes = sorted({m[1] for m in matches}, reverse=True)
    ultima = nomes[0]
    return ultima


def manter_cache_omie():
    nomes = listar_ficheiros_cache()
    if len(nomes) <= RETENCAO_CACHE:
        return
    for nome in nomes[RETENCAO_CACHE:]:
        caminho = caminho_cache(nome)
        try:
            os.remove(caminho)
            logger.info(f"Cache removida: {nome}")
        except Exception as e:
            logger.error(f"Erro ao remover cache {nome}: {e}")


def ler_estado_consumo_shelly():
    try:
        if not os.path.exists(SHELLY_CONSUMPTION_STATE):
            return {}
        with open(SHELLY_CONSUMPTION_STATE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Erro ao ler estado Shelly: {e}")
        return {}


def gravar_estado_consumo_shelly(estado):
    try:
        with open(SHELLY_CONSUMPTION_STATE, 'w', encoding='utf-8') as f:
            json.dump(estado, f, indent=2)
    except Exception as e:
        logger.error(f"Erro ao gravar estado Shelly: {e}")


def calcular_consumo_diario(total_wh, preco_kwh):
    agora = datetime.now()
    hoje = agora.strftime('%Y-%m-%d')
    total_wh = float(total_wh or 0)

    estado = ler_estado_consumo_shelly()
    if estado.get('current_day') != hoje:
        estado = {
            'current_day': hoje,
            'day_start_total_wh': total_wh,
            'last_total_wh': total_wh,
            'daily_consumed_wh': 0.0,
        }
    else:
        ultimo_total = float(estado.get('last_total_wh', total_wh))
        dia_inicio = float(estado.get('day_start_total_wh', total_wh))

        if total_wh < ultimo_total:
            logger.warning(f"Reset detetado no Shelly: {ultimo_total} -> {total_wh} WH")
            dia_inicio = total_wh
            diario_wh = 0.0
        else:
            diario_wh = total_wh - dia_inicio
            if diario_wh < 0:
                diario_wh = 0.0
            elif diario_wh > 200000:
                logger.warning(f"Consumo suspeito ({diario_wh} WH), a reiniciar base.")
                dia_inicio = total_wh
                diario_wh = 0.0

        estado['daily_consumed_wh'] = diario_wh
        estado['last_total_wh'] = total_wh
        estado['day_start_total_wh'] = dia_inicio

    gravar_estado_consumo_shelly(estado)
    consumo_kwh = estado['daily_consumed_wh'] / 1000.0
    custo_eur = consumo_kwh * preco_kwh
    return round(consumo_kwh, 3), round(custo_eur, 4)


def baixar_ultimo_ficheiro_omie(nome_ficheiro):
    garantir_pasta_cache()
    url = OMIE_DOWNLOAD_BASE + urllib.parse.quote(nome_ficheiro, safe='')
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as response:
            conteudo = response.read()

        caminho = caminho_cache(nome_ficheiro)
        tmp = caminho + ".tmp"
        with open(tmp, 'wb') as f:
            f.write(conteudo)
        os.replace(tmp, caminho)
        gravar_nome_meta_local(nome_ficheiro)
        manter_cache_omie()
        logger.info(f"Ficheiro OMIE atualizado: {nome_ficheiro}")
        return True
    except Exception as e:
        logger.error(f"Erro ao descarregar OMIE {nome_ficheiro}: {e}")
        return False


def verificar_atualizacao_omie():
    ultima = obter_ultima_fonte_omie()
    if not ultima:
        return False

    atual = ler_nome_meta_local()
    if atual == ultima and os.path.exists(caminho_cache(ultima)):
        logger.info(f"Cache OMIE já está atualizada ({atual})")
        return True

    logger.info(f"Nova versão OMIE disponível: {ultima}")
    return baixar_ultimo_ficheiro_omie(ultima)


def atualizar_cache_periodicamente():
    while True:
        verificar_atualizacao_omie()
        time.sleep(CACHE_CHECK_INTERVAL)

def obter_dados_shelly(ip):
    """Centraliza a lógica de busca de dados do hardware Shelly"""
    try:
        url = f"http://{ip}/status"
        with urllib.request.urlopen(url, timeout=5) as response:
            dados = json.loads(response.read().decode())
            emeter = dados.get('emeters', [{}])[0]
            return {
                "potencia": emeter.get('power', 0),
                "total_wh": emeter.get('total', 0)
            }
    except Exception as e:
        logger.error(f"Erro Shelly ({ip}): {e}")
        return {"potencia": 0, "total_wh": 0}

@app.get("/")
async def serve_index():
    """Serve o dashboard HTML"""
    index_path = os.path.join(ROOT_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="index.html não encontrado")

@app.get("/dados_energia")
async def get_dados_energia():
    dados_h = obter_dados_shelly(SHELLY_EM_IP)
    potencia = dados_h["potencia"]
    total_wh = dados_h["total_wh"]

    preco_omie_mwh = 50.0
    origem_preco = "fallback"
    
    preco_local = obter_preco_omie_local()
    if preco_local is not None:
        preco_omie_mwh = preco_local
        origem_preco = "local"
    else:
        # Tenta API online se local falhar
        try:
            with urllib.request.urlopen("https://api.precosdoomie.pt/v1/today", timeout=3) as r:
                dados_omie = json.loads(r.read().decode())
                preco_omie_mwh = dados_omie['hours'][datetime.now().hour]['price']
                origem_preco = "online"
        except:
            logger.error("Falha em todas as fontes de preço. Usando fallback.")

    previsao_omie = obter_previsao_omie_local()
    preco_kwh_base = preco_omie_mwh / 1000
    preco_final_kwh = ((preco_kwh_base * FADEQU * 1.15) + PERDAS_E_TAXAS + TARIFA_ACESSO) * IVA
    
    custo_por_hora = (potencia / 1000) * preco_final_kwh

    if potencia < -50:
        cor, motivo = "VERDE", f"Excedente Solar! A injetar {-int(potencia)}W"
    else:
        cor, motivo = obter_semaforo_por_preco(preco_final_kwh)

    consumo_diario_kwh, custo_diario_eur = calcular_consumo_diario(total_wh, preco_final_kwh)

    return {
        "potencia": potencia,
        "preco_kwh": round(preco_final_kwh, 3),
        "custo_por_hora": round(custo_por_hora, 4),
        "custo_por_minuto": round(custo_por_hora / 60, 6),
        "consumo_diario_kwh": consumo_diario_kwh,
        "custo_diario_eur": custo_diario_eur,
        "cor": cor,
        "motivo": motivo,
        "origem_preco": origem_preco,
        "previsao": previsao_omie
    }

if __name__ == '__main__':
    os.chdir(BASE_DIR)
    ip_local = obter_ip_local()
    logger.info(f"Dashboard disponível em: http://{ip_local}:{PORT}")

    verificar_atualizacao_omie()
    thread = threading.Thread(target=atualizar_cache_periodicamente, daemon=True)
    thread.start()

    uvicorn.run(app, host="0.0.0.0", port=PORT)