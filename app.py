import os
import requests
import json
import google.generativeai as genai
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2 # <- Adicionado para salvar leads
import traceback # <- Adicionado para logs de erro

# Carrega variáveis do .env (APENAS para testes locais)
load_dotenv() 

app = Flask(__name__)
CORS(app) 

# --- 1. Configuração Lida do Ambiente (Render) ---
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- 2. Configuração do Gemini (do Taurusbot) ---
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-flash-latest')
        print("✅  [Gemini] Modelo ('gemini-flash-latest') inicializado.")
    else:
        model = None
        print("❌ ERRO: GEMINI_API_KEY não encontrada. O Chatbot de Diagnóstico não funcionará.")
except Exception as e:
    model = None
    print(f"❌ Erro ao configurar a API do Gemini: {e}")
    traceback.print_exc()
    
# --- 3. [HELPER] Função de Análise PageSpeed (Otimizada) ---
def fetch_full_pagespeed_json(url_to_check, api_key):
    """
    Função helper que chama a API PageSpeed e retorna o JSON completo.
    """
    print(f"ℹ️  [PageSpeed] Iniciando análise para: {url_to_check}")
    
    # Otimizado: Pede apenas SEO e Performance para ser mais rápido
    categories = "category=SEO&category=PERFORMANCE"
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

# --- 4. [HELPER] Função para extrair falhas (Otimizada) ---
def extract_failing_audits(report_json):
    """
    Extrai uma lista de auditorias que falharam (score != 1).
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

# --- 5. [NOVO] Função para garantir que a tabela de leads exista ---
def setup_database():
    """
    Garante que a tabela 'leads_chatbot' exista no banco de dados
    ao iniciar o aplicativo.
    """
    conn = None
    try:
        if not DATABASE_URL:
            print("⚠️ AVISO [DB]: DATABASE_URL não configurada. A captura de leads está desabilitada.")
            return

        print("ℹ️  [DB] Conectando ao PostgreSQL para verificar tabela 'leads_chatbot'...")
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        # Comando SQL (Idempotente)
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
        print("✅  [DB] Tabela 'leads_chatbot' verificada/criada com sucesso.")
        
    except psycopg2.Error as e:
        print(f"❌ ERRO [DB] ao configurar a tabela 'leads_chatbot': {e}")
        if conn:
            conn.rollback()
    except Exception as e:
        print(f"❌ ERRO Inesperado [DB] em setup_database: {e}")
    finally:
        if conn:
            conn.close()
            print("🔌  [DB] Conexão de setup fechada.")

# --- 6. Endpoints da API ---

@app.route('/')
def index():
    return jsonify({"message": "Fluxo de Ouro API Service (V5 - Captura de Leads) is running"})

# --- Endpoint 1: Barra de Busca (Diagnóstico Rápido) ---
@app.route('/api/get-pagespeed', methods=['POST'])
def get_pagespeed_report():
    """Endpoint para o diagnóstico rápido da barra de busca."""
    print("\n--- Recebido trigger para /api/get-pagespeed ---")
    
    if not PAGESPEED_API_KEY:
        print("❌ ERRO: PAGESPEED_API_KEY não definida.")
        return jsonify({"status_message": "Erro: O servidor não está configurado."}), 500
    
    try:
        data = request.get_json()
        inspected_url = data.get('inspected_url')
        if not inspected_url:
            return jsonify({"status_message": "Erro: Nenhuma URL fornecida."}), 400
    except Exception as e:
        return jsonify({"status_message": f"Erro: Requisição mal formatada. {e}"}), 400

    results, error = fetch_full_pagespeed_json(inspected_url, PAGESPEED_API_KEY)
    
    if error:
        return jsonify({"status_message": error}), 502

    seo_score_raw = results.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score')
    
    if seo_score_raw is None:
         print("❌ ERRO: Resposta da API não continha 'score' de SEO.")
         return jsonify({"status_message": "Erro: Não foi possível extrair o score."}), 500

    seo_score = seo_score_raw * 100
    status_message = f"Diagnóstico Mobile: 🚀 SEO: {seo_score:.0f}/100."
    
    print(f"✅ Análise PageSpeed Rápida concluída: {status_message}")
    return jsonify({"status_message": status_message}), 200

# --- Endpoint 2: Chatbot (Diagnóstico ISCA com IA) ---
@app.route('/api/get-seo-diagnosis', methods=['POST'])
def get_seo_diagnosis():
    """
    Endpoint para o diagnóstico ISCA do Chatbot com Gemini.
    Não entrega a análise, apenas o resumo para capturar o lead.
    """
    print("\n--- Recebido trigger para /api/get-seo-diagnosis ---")
    
    if not PAGESPEED_API_KEY or not model:
        print("❌ ERRO: Chaves de API (PageSpeed ou Gemini) não definidas.")
        return jsonify({"error": "Erro: O servidor não está configurado para o diagnóstico de IA."}), 500

    try:
        data = request.get_json()
        user_url = data.get('user_url')
        if not user_url:
            return jsonify({"error": "Nenhuma URL fornecida."}), 400
    except Exception:
        return jsonify({"error": "Requisição mal formatada."}), 400

    try:
        # 1. Busca o relatório do usuário
        user_report, user_error = fetch_full_pagespeed_json(user_url, PAGESPEED_API_KEY)
        if user_error:
            return jsonify({"error": user_error}), 502

        # 2. Extrai as falhas e o score
        user_failing_audits = extract_failing_audits(user_report)
        user_seo_score = (user_report.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score', 0)) * 100

        # 3. Cria o System Prompt (V5 - Focado em Captura)
        system_prompt = f"""
        Você é o "Analista de Ouro", um especialista sênior em SEO.
        Sua missão é dar um DIAGNÓSTICO-ISCA para um usuário que enviou a URL do site dele.

        REGRAS:
        1.  **Tom de Voz:** Profissional, especialista, mas com senso de urgência. Use 🚀 e 💡.
        2.  **NÃO DÊ A SOLUÇÃO:** Seu objetivo NÃO é dar o diagnóstico completo, mas sim provar que você o encontrou e que ele é valioso.
        3.  **A ISCA:** Seu trabalho é analisar a lista de 'Auditorias com Falha' e o 'Score' do usuário e gerar um texto curto (2-3 parágrafos) que:
            a. Confirma a nota (ex: "💡 Certo, analisei o {user_url} e a nota de SEO mobile é {user_seo_score:.0f}/100.").
            b. Menciona a *quantidade* de falhas (ex: "Identifiquei **{len(user_failing_audits)} falhas técnicas** que estão impedindo seu site de performar melhor...").
            c. Cita 1 ou 2 *exemplos* de falhas (ex: "...incluindo problemas com `meta descriptions` e imagens não otimizadas.").
            d. **O GANCHO (IMPORTANTE):** Termine induzindo o usuário a fornecer os dados para receber a análise completa.
        4.  **FORMULÁRIO DE CAPTURA:** O seu texto DEVE terminar exatamente com o comando para o frontend exibir o formulário. Use a tag especial: [FORMULARIO_LEAD]

        EXEMPLO DE RESPOSTA PERFEITA:
        "💡 Certo, analisei o {user_url} e a nota de SEO mobile é **{user_seo_score:.0f}/100**.

        Identifiquei **{len(user_failing_audits)} falhas técnicas** que estão impedindo seu site de alcançar a nota 100/100, incluindo problemas com `meta descriptions` e imagens que não estão otimizadas para mobile.

        Eu preparei um relatório detalhado com o "como corrigir" para cada um desses {len(user_failing_audits)} pontos. Por favor, preencha os campos abaixo para eu enviar a análise completa para você:
        [FORMULARIO_LEAD]"
        
        ---
        ANÁLISE DO SITE DO USUÁRIO ({user_url}):
        - Score Geral de SEO: {user_seo_score:.0f}/100
        - Auditorias com Falha: {json.dumps(user_failing_audits, ensure_ascii=False)}
        ---
        
        DIAGNÓSTICO-ISCA (comece aqui):
        """
        
        chat_session = model.start_chat(history=[])
        response = chat_session.send_message(
            system_prompt,
            generation_config=genai.types.GenerationConfig(temperature=0.3),
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE', 'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'}
        )

        print(f"🤖 [Gemini] Diagnóstico-ISCA gerado: {response.text[:100]}...")
        # Adiciona o score ao JSON de resposta, para o JS poder usar
        return jsonify({'diagnosis': response.text, 'seo_score': user_seo_score})

    except Exception as e:
        print(f"❌ ERRO Inesperado em /api/get-seo-diagnosis: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Ocorreu um erro ao gerar o diagnóstico de IA.'}), 500

# --- Endpoint 3: [NOVO] Captura do Lead ---
@app.route('/api/capture-lead', methods=['POST'])
def capture_lead():
    """
    Endpoint para salvar os dados do lead (Nome, E-mail, WhatsApp) no banco.
    """
    print("\n--- Recebido trigger para /api/capture-lead ---")
    
    if not DATABASE_URL:
        print("❌ ERRO [DB]: DATABASE_URL não definida. Não é possível salvar o lead.")
        return jsonify({"error": "Erro interno do servidor."}), 500
        
    try:
        data = request.get_json()
        nome = data.get('nome')
        email = data.get('email')
        whatsapp = data.get('whatsapp')
        url_analisada = data.get('url_analisada')
        score_seo = data.get('score_seo')

        if not nome or not email or not url_analisada:
            return jsonify({"error": "Nome, E-mail e URL são obrigatórios."}), 400

    except Exception:
        return jsonify({"error": "Requisição mal formatada."}), 400

    conn = None
    try:
        print(f"ℹ️  [DB] Salvando lead: {nome} ({email}) para a URL: {url_analisada}")
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO leads_chatbot (nome, email, whatsapp, url_analisada, score_seo)
            VALUES (%s, %s, %s, %s, %s)
        """, (nome, email, whatsapp, url_analisada, score_seo))
        
        conn.commit()
        cur.close()
        
        print("✅  [DB] Lead salvo com sucesso.")
        return jsonify({"success": "Lead salvo com sucesso!"}), 201

    except Exception as e:
        print(f"❌ ERRO [DB] ao salvar o lead: {e}")
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro ao salvar o lead no banco de dados."}), 500
    finally:
        if conn:
            conn.close()

# --- Execução do App (Pronto para Render/Gunicorn) ---
if __name__ == "__main__":
    setup_database() # Garante que a tabela exista ANTES de rodar o app
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

