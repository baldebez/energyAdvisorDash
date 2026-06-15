import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import socket
import os
import re
import threading
import time
from datetime import datetime, timedelta

# --- CONFIGURAÇÕES ---
PORT = 8080
SHELLY_EM_IP = "192.168.68.105"  # SUBSTITUA PELO IP REAL DO SEU SHELLY EM
TARIFA_ACESSO = 0.06         # Fora de Vazio em Bi-horário (Mude se necessário)
FADEQU = 1.02
PERDAS_E_TAXAS = 0.0155
IVA = 1.23
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
        print(f"Nao foi possivel criar pasta de cache: {e}")


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

    print(f"Ficheiros locais OMIE: {arquivos}")

    for nome in arquivos:
        caminho = caminho_cache(nome) if nome != FICHEIRO_PRECOS else nome
        try:
            with open(caminho, 'r', encoding='utf-8') as f:
                linhas = f.readlines()
        except Exception as e:
            print(f"Nao foi possivel ler {caminho}: {e}")
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
            print(f"Preco OMIE obtido do ficheiro local: {preco} €/MWh (hora {dt.strftime('%Y-%m-%d %H:%M')})")
            return preco

    fallback = None
    for dt, preco in reversed(entradas):
        if dt <= agora:
            fallback = (dt, preco)
            break
    if fallback:
        dt, preco = fallback
        print(f"Hora exata nao encontrada. Usando preco OMIE de {dt.strftime('%Y-%m-%d %H:%M')}: {preco} €/MWh")
        return preco

    dt, preco = entradas[0]
    print(f"Hora nao encontrada no ficheiro. Usando primeiro preco disponivel: {preco} €/MWh ({dt.strftime('%Y-%m-%d %H:%M')})")
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
        print(f"Erro ao ler meta local: {e}")
        return None


def gravar_nome_meta_local(nome):
    try:
        with open(META_FILE, 'w', encoding='utf-8') as f:
            f.write(nome)
    except Exception as e:
        print(f"Erro ao gravar meta local: {e}")


def obter_ultima_fonte_omie():
    try:
        req = urllib.request.Request(OMIE_LIST_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"Erro ao consultar lista OMIE: {e}")
        return None

    html = html.replace('&amp;', '&')
    pattern = re.compile(r'href=["\'](/pt/file-download\?parents=marginalpdbcpt&filename=(marginalpdbcpt_\d{8}\.1))["\']', re.IGNORECASE)
    matches = pattern.findall(html)
    if not matches:
        primeiro = html.find('file-download')
        snippet = html[primeiro - 150:primeiro + 250] if primeiro != -1 else html[:400]
        print("Nao foi possivel extrair o ficheiro mais recente da lista OMIE")
        print(f"Snippet OMIE: {snippet}")
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
            print(f"Removido ficheiro antigo de cache: {nome}")
        except Exception as e:
            print(f"Erro ao remover ficheiro antigo {nome}: {e}")


def ler_estado_consumo_shelly():
    try:
        if not os.path.exists(SHELLY_CONSUMPTION_STATE):
            return {}
        with open(SHELLY_CONSUMPTION_STATE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Erro ao ler estado de consumo Shelly: {e}")
        return {}


def gravar_estado_consumo_shelly(estado):
    try:
        with open(SHELLY_CONSUMPTION_STATE, 'w', encoding='utf-8') as f:
            json.dump(estado, f, indent=2)
    except Exception as e:
        print(f"Erro ao gravar estado de consumo Shelly: {e}")


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
            print(f"Detetado reset do contador Shelly: ultimo={ultimo_total} WH, atual={total_wh} WH")
            dia_inicio = total_wh
            diario_wh = 0.0
        else:
            diario_wh = total_wh - dia_inicio
            if diario_wh < 0:
                diario_wh = 0.0
            elif diario_wh > 200000:
                print(f"Valor de consumo diário suspeito: {diario_wh} WH; reiniciando base. total_wh={total_wh}, dia_inicio={dia_inicio}")
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
        print(f"Ficheiro OMIE atualizado: {nome_ficheiro}")
        return True
    except Exception as e:
        print(f"Erro ao descarregar ficheiro OMIE {nome_ficheiro}: {e}")
        return False


def verificar_atualizacao_omie():
    ultima = obter_ultima_fonte_omie()
    if not ultima:
        return False

    atual = ler_nome_meta_local()
    if atual == ultima and os.path.exists(caminho_cache(ultima)):
        print(f"Cache OMIE atual ja esta em {atual}")
        return True

    print(f"Nova versao OMIE disponivel: {ultima} (atual: {atual or 'nenhuma'})")
    return baixar_ultimo_ficheiro_omie(ultima)


def atualizar_cache_periodicamente():
    while True:
        verificar_atualizacao_omie()
        time.sleep(CACHE_CHECK_INTERVAL)

class SemaforoHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/dados_energia':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*') # Resolve o CORS
            self.end_headers()
            
            # 1. Obter Potência em tempo real do Shelly EM
            potencia = 0
            total_wh = 0
            try:
                # Na geração 1 do Shelly EM o endpoint é /status. Na Gen2/3 é /rpc/EM.GetStatus?id=0
                # Ajuste o URL conforme a geração do seu Shelly
                with urllib.request.urlopen(f"http://{SHELLY_EM_IP}/status", timeout=10) as response:
                    dados_shelly = json.loads(response.read().decode())
                    potencia = dados_shelly['emeters'][0].get('power', 0)
                    total_wh = dados_shelly['emeters'][0].get('total', 0)
            except Exception as e:
                print(f"Erro ao ler Shelly EM: {e}")

            # 2. Obter Preço Atual do OMIE (Tentando: ficheiro local → APIs online)
            preco_omie_mwh = 50.0  # Valor de segurança padrão
            origem_preco = "fallback"
            
            # Primeira tentativa: Ficheiro local
            preco_local = obter_preco_omie_local()
            if preco_local is not None:
                preco_omie_mwh = preco_local
                origem_preco = "local"
            else:
                # Se ficheiro local falhar, tentar APIs online
                print("   Tentando obter preço de APIs online...")
                apis_omie = [
                    "https://api.precosdoomie.pt/v1/today",
                    "https://www.omie.pt/api/public/preco-medio-horario",
                ]
                
                for url_api in apis_omie:
                    try:
                        print(f"   Tentando: {url_api}")
                        with urllib.request.urlopen(url_api, timeout=3) as response:
                            dados_omie = json.loads(response.read().decode())
                            # Tenta vários formatos de resposta
                            if 'hours' in dados_omie:
                                hora_atual = datetime.now().hour
                                preco_omie_mwh = dados_omie['hours'][hora_atual]['price']
                            elif 'price' in dados_omie:
                                preco_omie_mwh = dados_omie['price']
                            elif isinstance(dados_omie, list) and len(dados_omie) > 0:
                                hora_atual = datetime.now().hour
                                if hora_atual < len(dados_omie):
                                    preco_omie_mwh = dados_omie[hora_atual]
                            
                            origem_preco = "online"
                            print(f"Preco OMIE (API online): {preco_omie_mwh} €/MWh")
                            break  # Sucesso, sair do loop
                            
                    except urllib.error.URLError as e:
                        print(f"   {url_api}: {e.reason}")
                    except json.JSONDecodeError:
                        print(f"   Resposta invalida de {url_api}")
                    except Exception as e:
                        print(f"   Erro: {type(e).__name__}")
                else:
                    # Se chegou aqui, nenhuma API funcionou
                    print(f"APIs offline. Usando preco fallback: {preco_omie_mwh} €/MWh")

            previsao_omie = obter_previsao_omie_local()

            # 3. Calcular Preço Final com a fórmula G9
            preco_kwh_base = preco_omie_mwh / 1000
            preco_final_kwh = ((preco_kwh_base * FADEQU * 1.15) + PERDAS_E_TAXAS + TARIFA_ACESSO) * IVA

            # 3.1 Calcular Custo Instantâneo
            potencia_kw = potencia / 1000
            custo_por_hora = potencia_kw * preco_final_kwh
            custo_por_minuto = custo_por_hora / 60

            # 4. Árvore de Decisão do Semáforo
            if potencia < -50:
                cor = "VERDE"
                motivo = f"Excedente Solar! A injetar {-int(potencia)}W"
            elif preco_final_kwh <= 0.12:
                cor = "VERDE"
                motivo = f"Energia da rede barata ({preco_final_kwh:.3f}€/kWh)"
            elif preco_final_kwh < 0.22:
                cor = "AMARELO"
                motivo = f"Preço moderado ({preco_final_kwh:.3f}€/kWh)"
            else:
                cor = "VERMELHO"
                motivo = f"PICO DE PREÇO! Evitar consumos ({preco_final_kwh:.3f}€/kWh)"

            consumo_diario_kwh, custo_diario_eur = calcular_consumo_diario(total_wh, preco_final_kwh)

            resposta = {
                "potencia": potencia,
                "preco_kwh": round(preco_final_kwh, 3),
                "custo_por_hora": round(custo_por_hora, 4),
                "custo_por_minuto": round(custo_por_minuto, 6),
                "consumo_diario_kwh": consumo_diario_kwh,
                "custo_diario_eur": custo_diario_eur,
                "cor": cor,
                "motivo": motivo,
                "origem_preco": origem_preco,
                "previsao": previsao_omie
            }
            
            self.wfile.write(json.dumps(resposta).encode())
        else:
            # Serve os ficheiros HTML normais que estiverem na mesma pasta
            super().do_GET()

if __name__ == '__main__':
    os.chdir(BASE_DIR)
    ip_local = obter_ip_local()
    print(f"Servidor do Semaforo a correr no porto {PORT}")
    print(f"No tablet, abra o browser em http://{ip_local}:8080")

    verificar_atualizacao_omie()
    thread = threading.Thread(target=atualizar_cache_periodicamente, daemon=True)
    thread.start()

    with socketserver.TCPServer(("", PORT), SemaforoHandler) as httpd:
        httpd.serve_forever()