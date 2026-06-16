import urllib.request
import urllib.parse
import json
import socket
import os
import re
import threading
import time
import logging
import ssl
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
DATA_DIR = os.path.join(ROOT_DIR, "data") # Pasta de dados ao lado de src
FICHEIRO_PRECOS_NAME = "precos_omie.txt"
FICHEIRO_PRECOS = os.path.join(DATA_DIR, FICHEIRO_PRECOS_NAME)  # Ficheiro local antigo/alternativo
CACHE_FOLDER = os.path.join(DATA_DIR, "cache_omie")
META_FILE = os.path.join(DATA_DIR, "precos_omie.meta")
SHELLY_CONSUMPTION_STATE = os.path.join(DATA_DIR, "shelly_consumo.json")
OMIE_LIST_URL = "https://www.omie.es/pt/file-access-list?parents=/%20Mercado%20Di%C3%A1rio/1.%20Pre%C3%A7os&dir=%20Pre%C3%A7os%20por%20hora%20do%20mercado%20di%C3%A1rio%20em%20Portugal&realdir=marginalpdbcpt"
OMIE_DOWNLOAD_BASE = "https://www.omie.es/pt/file-download?parents=marginalpdbcpt&filename="
CACHE_CHECK_INTERVAL = 3600  # segundos entre verificações de atualização
HORAS_PREVISAO = 12  # Quantas horas para mostrar no semáforo futuro
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
            # OMIE usa frequentemente latin-1 ou utf-8 com caracteres específicos
            with open(caminho, 'r', encoding='latin-1') as f:
                linhas = f.readlines()
        except Exception as e:
            logger.error(f"Nao foi possivel ler {caminho}: {e}")
            continue

        for linha in linhas:
            partes = linha.strip().split(';')
            if len(partes) < 6 or not partes[0].isdigit():
                continue
            try:
                ano = int(partes[0])
                mes = int(partes[1])
                dia = int(partes[2])
                periodo = int(partes[3]) # 1 a 96 (periodos de 15 min)
                # O preço de Portugal está na 6ª coluna (índice 5)
                preco = float(partes[5].replace(',', '.'))
            except Exception:
                continue

            # Converte o período (1-96) para o timestamp correto (00:00, 00:15, ...)
            dt = datetime(ano, mes, dia) + timedelta(minutes=(periodo - 1) * 15)
            entradas.append((dt, preco))

    entradas.sort(key=lambda x: x[0])
    print(f"Entradas localizadas: {len(entradas)}")
    return entradas


def encontrar_preco_em_data(dt_alvo, entradas):
    """Encontra o último preço disponível para um determinado momento"""
    preco_encontrado = None
    for dt_e, preco in entradas:
        if dt_e <= dt_alvo:
            preco_encontrado = preco
        else:
            break
    return preco_encontrado

def obter_preco_omie_local():
    entradas = parse_precos_omie_local()
    if not entradas:
        return None
    
    agora = datetime.now()
    preco = encontrar_preco_em_data(agora, entradas)
    if preco is not None:
        logger.info(f"Preco OMIE local encontrado: {preco} €/MWh")
        return preco
    
    return entradas[0][1] if entradas else None


def obter_previsao_omie_online():
    """Tenta obter a previsão diretamente da API JSON (mais fiável que o CSV)"""
    try:
        context = ssl._create_unverified_context()
        # Vamos buscar hoje e amanhã para garantir que o gráfico nunca fica vazio à noite
        previsao_final = []
        for dia in ["today", "tomorrow"]:
            url = f"https://api.precosdoomie.pt/v1/{dia}"
            try:
                with urllib.request.urlopen(url, timeout=5, context=context) as r:
                    dados = json.loads(r.read().decode())
                    for h_info in dados.get('hours', []):
                        # A API usa formato "00-01", "01-02"...
                        hora_inicio = int(h_info['hour'].split('-')[0])
                        # Criar datetime para o dia correspondente
                        dt = datetime.now() if dia == "today" else datetime.now() + timedelta(days=1)
                        dt = dt.replace(hour=hora_inicio, minute=0, second=0, microsecond=0)
                        
                        preco_mwh = float(h_info['price'])
                        preco_kwh_base = preco_mwh / 1000
                        preco_final_kwh = ((preco_kwh_base * FADEQU * 1.15) + PERDAS_E_TAXAS + TARIFA_ACESSO) * IVA
                        
                        cor, mensagem = obter_semaforo_por_preco(preco_final_kwh)
                        previsao_final.append({
                            "hora": dt.strftime('%d/%m %H:%M'),
                            "preco_kwh": round(preco_final_kwh, 3),
                            "cor": cor,
                            "motivo": mensagem,
                            "valor": preco_final_kwh,
                            "ts": dt.timestamp(),
                            "preco_original_mwh": round(preco_mwh, 2)
                        })
            except:
                continue
        return sorted(previsao_final, key=lambda x: x['ts'])
    except Exception as e:
        logger.error(f"Erro na previsao online: {e}")
        return []

def obter_previsao_omie_local(horas=HORAS_PREVISAO, intervalo_minutos=INTERVALO_PREVISAO_MIN):
    entradas = parse_precos_omie_local()
    # Obtém dados online como backup para preencher lacunas (ex: amanhã ainda não descarregado)
    previsao_online = obter_previsao_omie_online()
    
    # Mapeia a previsão online por hora para consulta rápida
    mapa_online = {p['ts']: p for p in previsao_online}

    agora = datetime.now()
    # Alinha o início da previsão com o múltiplo de 15 minutos anterior
    agora = agora.replace(minute=(agora.minute // 15) * 15, second=0, microsecond=0)
    
    previsao = []
    total_blocos = int(horas * 60 / intervalo_minutos)
    
    for i in range(total_blocos):
        bloco_dt = agora + timedelta(minutes=i * intervalo_minutos)
        bloco_ts = bloco_dt.timestamp()

        # 1. Tentar encontrar no CSV local (precisão de 15 min)
        preco_mwh = encontrar_preco_em_data(bloco_dt, entradas)
        
        if preco_mwh is not None:
            preco_kwh_base = preco_mwh / 1000
            preco_final_kwh = ((preco_kwh_base * FADEQU * 1.15) + PERDAS_E_TAXAS + TARIFA_ACESSO) * IVA
            cor, mensagem = obter_semaforo_por_preco(preco_final_kwh)
            previsao.append({
                "hora": bloco_dt.strftime('%d/%m %H:%M'),
                "preco_kwh": round(preco_final_kwh, 3),
                "cor": cor,
                "motivo": mensagem,
                "valor": preco_final_kwh,
                "ts": bloco_ts,
                "preco_original_mwh": round(preco_mwh, 2)
            })
        else:
            # 2. Se não houver no local, procurar na fonte online (arredondando para a hora)
            ts_hora = bloco_dt.replace(minute=0, second=0, microsecond=0).timestamp()
            if ts_hora in mapa_online:
                item_online = mapa_online[ts_hora].copy()
                # Ajustamos os metadados do item online para corresponder ao bloco de 15m
                item_online["hora"] = bloco_dt.strftime('%d/%m %H:%M')
                item_online["ts"] = bloco_ts
                previsao.append(item_online)
            else:
                # 3. Se não houver em lado nenhum, saltamos o bloco
                continue

    return previsao


def obter_semaforo_por_preco(preco_final_kwh):
    if preco_final_kwh <= 0.12:
        return "VERDE", "Preço Económico"
    elif preco_final_kwh < 0.22:
        return "AMARELO", "Preço Moderado"
    else:
        return "VERMELHO", "Pico de Preço"


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
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=15, context=context) as response:
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
        # Se mudou o dia, o início de hoje é o último valor que tínhamos ontem
        ultimo_conhecido = float(estado.get('last_total_wh', total_wh))
        estado = {
            'current_day': hoje,
            'day_start_total_wh': ultimo_conhecido,
            'last_total_wh': total_wh,
            'daily_consumed_wh': max(0.0, total_wh - ultimo_conhecido),
        }

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
        elif diario_wh > 200000: # Proteção contra saltos gigantes (ex: troca de hardware)
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
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=20, context=context) as response:
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
    # Tenta encontrar o index.html em vários locais possíveis
    caminhos_possiveis = [
        os.path.join(ROOT_DIR, "index.html"),
        os.path.join(BASE_DIR, "index.html")
    ]
    for caminho in caminhos_possiveis:
        if os.path.exists(caminho):
            return FileResponse(caminho)
    raise HTTPException(status_code=404, detail=f"index.html não encontrado. Verifique se o ficheiro está em {ROOT_DIR}")

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
            context = ssl._create_unverified_context()
            with urllib.request.urlopen("https://api.precosdoomie.pt/v1/today", timeout=3, context=context) as r:
                dados_omie = json.loads(r.read().decode())
                preco_omie_mwh = dados_omie['hours'][datetime.now().hour]['price']
                origem_preco = "online"
        except:
            logger.error("Falha em todas as fontes de preço. Usando fallback.")

    # Tenta obter previsão (Local com fallback automático para Online)
    previsao_omie = obter_previsao_omie_local()
    agora_base = datetime.now()
    
    # Filtrar para mostrar apenas do "agora" em diante
    agora_ts = agora_base.timestamp()
    previsao_omie = [p for p in previsao_omie if p.get('ts', 0) >= agora_ts - 900][:48] # 48 blocos = 12h

    preco_kwh_base = preco_omie_mwh / 1000
    preco_final_kwh = ((preco_kwh_base * FADEQU * 1.15) + PERDAS_E_TAXAS + TARIFA_ACESSO) * IVA

    # Gera o detalhe do cálculo para o tooltip
    detalhe_preco = (
        f"(({preco_kwh_base:.4f}€ [Base] * {FADEQU} [FADEQU] * 1.15 [Ajuste]) + "
        f"{PERDAS_E_TAXAS} [Perdas] + {TARIFA_ACESSO} [Acesso]) * {IVA} [IVA]"
    )

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
        "previsao": previsao_omie,
        "detalhe_preco": detalhe_preco
    }

if __name__ == '__main__':
    os.chdir(BASE_DIR)
    ip_local = obter_ip_local()
    logger.info(f"Dashboard disponível em: http://{ip_local}:{PORT}")
    
    # Verificação preventiva do index.html
    index_check = os.path.join(ROOT_DIR, "index.html")
    if not os.path.exists(index_check):
        logger.warning(f"AVISO: index.html não detectado em {index_check}. O dashboard pode falhar ao carregar.")

    verificar_atualizacao_omie()
    thread = threading.Thread(target=atualizar_cache_periodicamente, daemon=True)
    thread.start()

    uvicorn.run(app, host="0.0.0.0", port=PORT)