import os
import requests
import json
import google.generativeai as genai
import psycopg2 # Importado para salvar leads
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

# Carrega variáveis do .env (APENAS para testes locais)
load_dotenv() 

app = Flask(__name__)
CORS(app) 

# --- 1. Configuração Lida do Ambiente (Render) ---
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL") # Agora será usado ativamente
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- 2. Configuração do Gemini ---
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-flash-latest')
        print("✅  Modelo Gemini ('gemini-flash-latest') inicializado.")
    else:
        model = None
        print("❌ ERRO: GEMINI_API_KEY não encontrada. O Chatbot de Diagnóstico não funcionará.")
except Exception as e:
    model = None
    print(f"❌ Erro ao configurar a API do Gemini: {e}")

# --- 3. [HELPER] Funções de PageSpeed (As mesmas do V4) ---
def fetch_full_pagespeed_json(url_to_check, api_key):
    """
    Função helper que chama a API PageSpeed e retorna o JSON completo.
    """
    print(f"ℹ️  [PageSpeed] Iniciando análise para: {url_to_check}")
    categories = "category=SEO&category=PERFORMANCE&category=ACCESSIBILITY&category=BEST_PRACTICES"
    api_url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url={url_to_check}&key={api_key}&{categories}&strategy=MOBILE"
    
    try:
        response = requests.get(api_url, timeout=45) 
        response.raise_for_status() 
        results = response.json()
        print(f"✅  [PageSpeed] Análise de {url_to_check} concluída.")
        return results, None
    except requests.exceptions.HTTPError as http_err:
        print(f"❌ ERRO HTTP [PageSpeed]: {http_err}")
        error_details = "Erro desconhecido"
        try:
            error_details = http_err.response.json().get('error', {}).get('message', 'Verifique a URL')
        except:
            pass
        return None, f"Erro: A API do Google falhou ({error_details})."
    except Exception as e:
        print(f"❌ ERRO Inesperado [PageSpeed]: {e}")
        return None, "Erro: Não foi possível analisar essa URL."

def extract_failing_audits(report_json):
    """
    Extrai uma lista de auditorias que falharam (score != 1) do relatório JSON.
    """
    audits = report_json.get('lighthouseResult', {}).get('audits', {})
    failed_audits = []
    
    for audit_key, audit_details in audits.items():
        if audit_details.get('scoreDisplayMode') != 'informative' and audit_details.get('score') is not None and audit_details.get('score') < 1:
            failed_audits.append({
                "title": audit_details.get('title'),
                "description": audit_details.get('description'),
                "score": audit_details.get('score')
            })
    print(f"ℹ️  [Parser] Extraídas {len(failed_audits)} auditorias com falha.")
    return failed_audits

# --- 4. [NOVO] Função de Setup do Banco de Dados ---
def setup_database():
    """
    Garante que a tabela de leads exista ao iniciar o app.
    O Colab já fez isso, mas esta é uma garantia de segurança.
    """
    conn = None
    if not DATABASE_URL:
        print("⚠️ AVISO: DATABASE_URL não definida. A captura de leads falhará.")
        return
        
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # Comando IDEMPOTENTE (só cria se não existir)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads_chatbot (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255),
                email VARCHAR(255),
                whatsapp VARCHAR(50),
                url_analisada TEXT,
                score_seo INTEGER,
                data_captura TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        print("✅ Tabela 'leads_chatbot' verificada/criada com sucesso.")
    except Exception as e:
        print(f"❌ ERRO ao tentar criar tabela 'leads_chatbot': {e}")
    finally:
        if conn:
            conn.close()

# --- 5. Endpoints da API ---

@app.route('/')
def index():
    return jsonify({"message": "Fluxo de Ouro API Service (V5 - Captura de Leads) is running"})

# --- Endpoint 1: Barra de Busca (Diagnóstico Rápido) ---
# (Sem alteração, continua funcionando)
@app.route('/api/get-pagespeed', methods=['POST'])
def get_pagespeed_report():
    """Endpoint para o diagnóstico rápido da barra de busca."""
    print("\n--- Recebido trigger para /api/get-pagespeed ---")
    
    if not PAGESPEED_API_KEY:
        return jsonify({"status_message": "Erro: O servidor não está configurado."}), 500
    
    try:
        data = request.get_json()
        inspected_url = data.get('inspected_url')
    except Exception:
        return jsonify({"status_message": "Erro: Requisição mal formatada."}), 400

    results, error = fetch_full_pagespeed_json(inspected_url, PAGESPEED_API_KEY)
    if error: return jsonify({"status_message": error}), 502

    seo_score_raw = results.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score')
    if seo_score_raw is None:
         return jsonify({"status_message": "Erro: Não foi possível extrair o score."}), 500

    seo_score = seo_score_raw * 100
    status_message = f"Diagnóstico Mobile: 🚀 SEO: {seo_score:.0f}/100."
    
    print(f"✅ Análise PageSpeed Rápida concluída: {status_message}")
    return jsonify({"status_message": status_message}), 200

# --- Endpoint 2: Chatbot (Diagnóstico com IA - V5 com Captura) ---
@app.route('/api/get-seo-diagnosis', methods=['POST'])
def get_seo_diagnosis():
    """
    Endpoint para o diagnóstico do Chatbot.
    V5: Gera um RESUMO-ISCA e pede os dados do lead.
    """
    print("\n--- Recebido trigger para /api/get-seo-diagnosis ---")
    
    if not PAGESPEED_API_KEY or not model:
        print("❌ ERRO: API Keys (PageSpeed ou Gemini) não configuradas.")
        return jsonify({"error": "Erro: O servidor não está configurado para o diagnóstico de IA."}), 500

    try:
        data = request.get_json()
        user_url = data.get('user_url')
    except Exception:
        return jsonify({"error": "Requisição mal formatada."}), 400

    try:
        user_report, user_error = fetch_full_pagespeed_json(user_url, PAGESPEED_API_KEY)
        if user_error:
            return jsonify({"error": user_error}), 502

        user_failing_audits = extract_failing_audits(user_report)
        user_seo_score = (user_report.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score', 0)) * 100

        # 5. [V5] System Prompt focado em CAPTURA DE LEAD
        system_prompt = f"""
        Você é o "Analista de Ouro", um especialista sênior em SEO.
        Sua missão é dar um *resumo-isca* (teaser) para o usuário, com o objetivo de capturar o lead.

        REGRAS:
        1.  **Tom de Voz:** Profissional, especialista, mas com senso de urgência.
        2.  **A Isca:** Você **NÃO** vai entregar a análise completa agora.
        3.  **Análise (O que fazer):** Olhe o score de SEO e o número de falhas.
        4.  **O Script (O que dizer):**
            - Comece com o score (ex: "🚀 Certo. Analisei a {user_url} e o score de SEO Mobile dela é **{user_seo_score:.0f}/100**.")
            - Diga o número de falhas (ex: "Encontrei **{len(user_failing_audits)}** pontos de auditoria que precisam de atenção imediata (performance, acessibilidade e SEO).")
            - **O Gancho (CTA):** Peça os dados para a análise completa. Diga: "Para receber a análise completa e as dicas de correção por e-mail, por favor, me informe seu **Nome, E-mail e WhatsApp**."
        
        ---
        ANÁLISE DO SITE DO USUÁRIO ({user_url}):
        - Score Geral de SEO: {user_seo_score:.0f}/100
        - Número de Falhas: {len(user_failing_audits)}
        ---
        
        RESUMO-ISCA (comece aqui):
        """
        
        chat_session = model.start_chat(history=[])
        response = chat_session.send_message(
            system_prompt,
            generation_config=genai.types.GenerationConfig(temperature=0.5),
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE',
                             'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'}
        )

        print(f"🤖 [Gemini] Resumo-Isca gerado: {response.text[:100]}...")
        # Retorna o score junto para o frontend salvar
        return jsonify({
            'diagnosis': response.text,
            'score_seo': int(user_seo_score) # Envia o score para o frontend
        })

    except Exception as e:
        print(f"❌ ERRO Inesperado em /api/get-seo-diagnosis: {e}")
        return jsonify({'error': 'Ocorreu um erro ao gerar o diagnóstico de IA.'}), 500

# --- [NOVO] Endpoint 3: Captura de Lead ---
@app.route('/api/capture-lead', methods=['POST'])
def capture_lead():
    """
    Salva os dados do lead (Nome, Email, WhatsApp) no banco de dados.
    """
    print("\n--- Recebido trigger para /api/capture-lead ---")
    
    if not DATABASE_URL:
        print("❌ ERRO: DATABASE_URL não definida.")
        return jsonify({"error": "Configuração do servidor incompleta."}), 500

    try:
        data = request.get_json()
        nome = data.get('nome')
        email = data.get('email')
        whatsapp = data.get('whatsapp')
        url_analisada = data.get('url_analisada')
        score_seo = data.get('score_seo')

        if not email or not nome:
            return jsonify({"error": "Nome e E-mail são obrigatórios."}), 400

        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO leads_chatbot (nome, email, whatsapp, url_analisada, score_seo)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (nome, email, whatsapp, url_analisada, score_seo)
            )
            conn.commit()
            cur.close()
            print(f"✅ Lead salvo no banco de dados: {email}")
            
            # Resposta de agradecimento (o chatbot vai mostrar isso)
            return jsonify({"reply": f"Obrigado, {nome}! Lead registrado. Nossa equipe enviará a análise completa para o seu e-mail em breve."}), 201

        except Exception as e:
            if conn: conn.rollback()
            print(f"❌ ERRO ao salvar lead no DB: {e}")
            return jsonify({"error": "Não foi possível salvar o lead no banco de dados."}), 500
        finally:
            if conn: conn.close()
    
    except Exception as e:
        print(f"❌ ERRO em /api/capture-lead: {e}")
        return jsonify({"error": "Erro interno ao processar o lead."}), 500

# --- Execução do App (Pronto para Render/Gunicorn) ---
if __name__ == "__main__":
    # Garante que a tabela exista ao iniciar
    setup_database() 
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

