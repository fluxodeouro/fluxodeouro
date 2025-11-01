import os
import requests
import json
import google.generativeai as genai
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2 
import traceback
import threading # <- 1. Importado para background tasks

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

# --- 5. [NOVO] Função de Background (A TAREFA LENTA) ---
def generate_and_save_report(app_instance, lead_id, user_url, nome):
    """
    Esta função roda em uma thread separada (background).
    Ela executa as chamadas lentas (PageSpeed, Gemini) e salva o 
    resultado final no banco de dados.
    """
    # 'with app.app_context()' é essencial para a thread
    # acessar as variáveis de ambiente e configurações do Flask.
    with app_instance.app_context():
        print(f"ℹ️  [Thread-{lead_id}] Iniciando análise em background para {user_url}")
        conn_thread = None
        try:
            # 0. Pega as chaves de API (necessário dentro do contexto da thread)
            PAGESPEED_API_KEY_THREAD = os.environ.get("PAGESPEED_API_KEY")
            DATABASE_URL_THREAD = os.environ.get("DATABASE_URL")
            GEMINI_API_KEY_THREAD = os.environ.get("GEMINI_API_KEY")

            if not all([PAGESPEED_API_KEY_THREAD, DATABASE_URL_THREAD, GEMINI_API_KEY_THREAD]):
                raise Exception("Variáveis de ambiente não encontradas na thread.")

            # 1. Conecta ao DB (conexão exclusiva da thread)
            conn_thread = psycopg2.connect(DATABASE_URL_THREAD)
            cur_thread = conn_thread.cursor()

            # 2. Marca o lead como 'PROCESSANDO'
            cur_thread.execute("UPDATE leads_chatbot SET status_analise = 'PROCESSANDO' WHERE id = %s", (lead_id,))
            conn_thread.commit()

            # 3. Busca o relatório do PageSpeed (LENTO)
            report_json, report_error = fetch_full_pagespeed_json(user_url, PAGESPEED_API_KEY_THREAD)
            if report_error:
                raise Exception(f"Erro PageSpeed: {report_error}")

            # 4. Extrai falhas
            failing_audits = extract_failing_audits(report_json)
            seo_score = (report_json.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score', 0)) * 100

            # 5. Configura Gemini (instância da thread)
            genai.configure(api_key=GEMINI_API_KEY_THREAD)
            model_thread = genai.GenerativeModel('gemini-flash-latest')

            # 6. Cria o Prompt para o RELATÓRIO FINAL
            system_prompt_final = f"""
            Você é o "Analista de Ouro", um especialista sênior em SEO.
            Sua missão é gerar um RELATÓRIO COMPLETO E DETALHADO para o {nome}, que enviou os dados para analisar o site {user_url}.

            REGRAS:
            1.  **Tom de Voz:** Profissional, técnico, mas didático.
            2.  **FOCO NA SOLUÇÃO:** O usuário ( {nome} ) já é um lead. Seu objetivo é entregar valor.
            3.  **ESTRUTURA:**
                a. Comece com "Olá, {nome}! Aqui está seu diagnóstico completo para {user_url}."
                b. Confirme a nota: (ex: "Seu score de SEO mobile é {seo_score:.0f}/100.").
                c. Liste as falhas mais importantes e **explique de forma didática como corrigir CADA UMA DELAS**.
                d. Dê uma conclusão e próximos passos.
            4.  Use Markdown para formatar (listas, negrito, etc).

            ---
            ANÁLISE DO SITE ({user_url}):
            - Score Geral de SEO: {seo_score:.0f}/100
            - Auditorias com Falha: {json.dumps(failing_audits, ensure_ascii=False)}
            ---

            RELATÓRIO COMPLETO (comece aqui):
            """

            # 7. Chama a Gemini (LENTO)
            response = model_thread.start_chat(history=[]).send_message(
                system_prompt_final,
                generation_config=genai.types.GenerationConfig(temperature=0.5),
                safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE', 'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'}
            )
            final_report_text = response.text

            # 8. Salva o relatório final no Banco
            cur_thread.execute(
                "UPDATE leads_chatbot SET status_analise = 'CONCLUIDO', relatorio_final = %s WHERE id = %s",
                (final_report_text, lead_id)
            )
            conn_thread.commit()
            print(f"✅  [Thread-{lead_id}] Relatório final salvo com sucesso.")

        except Exception as e:
            print(f"❌ ERRO [Thread-{lead_id}]: {e}")
            traceback.print_exc()
            if conn_thread:
                # Salva a mensagem de erro no relatório
                error_msg = f"Falha ao gerar o relatório: {str(e)}"
                cur_thread.execute(
                    "UPDATE leads_chatbot SET status_analise = 'FALHA', relatorio_final = %s WHERE id = %s",
                    (error_msg, lead_id)
                )
                conn_thread.commit()
        finally:
            if conn_thread:
                cur_thread.close()
                conn_thread.close()
                print(f"🔌  [Thread-{lead_id}] Conexão de background fechada.")


# --- 6. [HELPER] Função de Setup do Banco ---
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
        
        # Comando SQL (Idempotente) - ADICIONADO colunas novas
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads_chatbot (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255),
                email VARCHAR(255),
                whatsapp VARCHAR(50),
                url_analisada TEXT,
                score_seo INTEGER,
                data_captura TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status_analise VARCHAR(50) DEFAULT 'PENDENTE',
                relatorio_final TEXT
            );
        """)
        
        # Garante que as colunas existam mesmo se a tabela já existia
        cur.execute("""
            ALTER TABLE leads_chatbot
                ADD COLUMN IF NOT EXISTS status_analise VARCHAR(50) DEFAULT 'PENDENTE',
                ADD COLUMN IF NOT EXISTS relatorio_final TEXT;
        """)

        conn.commit()
        cur.close()
        print("✅  [DB] Tabela 'leads_chatbot' (com colunas de status) verificada/criada com sucesso.")
        
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

# --- 7. Endpoints da API ---

@app.route('/')
def index():
    return jsonify({"message": "Fluxo de Ouro API Service (V5 - Captura de Leads com Threading) is running"})

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
    Este endpoint continua sendo LENTO, pois precisa gerar a ISCA.
    A tarefa de background só começa DEPOIS que o lead é capturado.
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
        # 1. Busca o relatório do usuário (LENTO)
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
        
        # 4. Chama a Gemini (LENTO)
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

# --- Endpoint 3: [MODIFICADO] Captura do Lead ---
@app.route('/api/capture-lead', methods=['POST'])
def capture_lead():
    """
    Endpoint para salvar os dados do lead (Nome, E-mail, WhatsApp) no banco.
    Esta rota agora é RÁPIDA. Ela salva o lead com status 'PENDENTE'
    e dispara a thread em background 'generate_and_save_report'
    para fazer o trabalho lento (PageSpeed + Relatório Final da Gemini).
    """
    print("\n--- Recebido trigger para /api/capture-lead (com Threading) ---")
    
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
        
        # SQL modificado para retornar o ID do novo lead
        cur.execute("""
            INSERT INTO leads_chatbot (nome, email, whatsapp, url_analisada, score_seo)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id;
        """, (nome, email, whatsapp, url_analisada, score_seo))
        
        # --- MODIFICAÇÃO PARA THREADING ---
        # 1. Pega o ID do lead que acabamos de criar
        lead_id = cur.fetchone()[0] 
        conn.commit() # Confirma o INSERT imediatamente
        
        print(f"✅  [DB] Lead salvo com ID: {lead_id}. Disparando análise em background...")

        # 2. Inicia a thread em background para o trabalho lento
        # Passamos 'app' (a instância do Flask) para a thread poder
        # criar seu próprio contexto.
        thread = threading.Thread(
            target=generate_and_save_report, 
            args=(app, lead_id, url_analisada, nome)
        )
        thread.start()
        
        # 3. Retorna a resposta imediata para o usuário
        return jsonify({"success": "Obrigado! Recebemos seus dados. Estamos gerando seu relatório completo, isso pode levar alguns minutos."}), 201
        # --- FIM DA MODIFICAÇÃO ---

    except Exception as e:
        print(f"❌ ERRO [DB] ao salvar o lead: {e}")
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro ao salvar o lead no banco de dados."}), 500
    finally:
        if conn:
            cur.close()
            conn.close()

# --- Execução do App (Pronto para Render/Gunicorn) ---
if __name__ == "__main__":
    setup_database() # Garante que a tabela exista ANTES de rodar o app
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
