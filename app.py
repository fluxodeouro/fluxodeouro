# Fluxo de Ouro API Service (v6.0 - Vendedor-Consultor IA)
# Este app implementa o fluxo completo de captura, diagnóstico,
# qualificação, geração de isca (Padrão Ouro) e upsell (orçamento).
#
# CORREÇÃO v6.4: Adicionado endpoint /api/test-gemini para descobrir
#                qual modelo é realmente aceito pela API v1beta do ambiente.
#
import os
import requests
import json
import google.generativeai as genai
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2
import traceback
from psycopg2.extras import RealDictCursor # Para retornar dicts do DB
from google.api_core import exceptions # Para capturar o 404

load_dotenv()

app = Flask(__name__)
CORS(app)

# --- 1. Configuração Lida do Ambiente (Render) ---
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
N8N_SECRET_KEY = os.environ.get("N8N_SECRET_KEY", "sua-chave-secreta-padrao")
SALES_WEBHOOK_URL = os.environ.get("SALES_WEBHOOK_URL") # Webhook para N8N/Vendas

# --- 2. Configuração do Gemini ---
# (Manter o modelo de fallback para garantir que o resto do código funcione)
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        
        # O modelo usado será o 'gemini-pro' (melhor compatibilidade)
        model = genai.GenerativeModel('gemini-pro') 
        print("✅  [Gemini] Modelo ('gemini-pro') inicializado.")
        
    else:
        model = None
        print("❌ ERRO: GEMINI_API_KEY não encontrada. O Chatbot não funcionará.")
except Exception as e:
    model = None
    print(f"❌ Erro ao configurar a API do Gemini: {e}")
    traceback.print_exc()

# --- 3. [HELPER] Funções do Banco de Dados ---
# (Sem alterações)
def get_db_connection():
    """Helper para abrir uma conexão com o banco."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"❌ ERRO [DB] ao conectar: {e}")
        traceback.print_exc()
        return None

def get_lead_by_id(lead_id):
    """Busca um lead e retorna como um dicionário."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM leads_chatbot WHERE id = %s", (lead_id,))
            lead = cur.fetchone()
            return lead
    except Exception as e:
        print(f"❌ ERRO [DB] ao buscar lead {lead_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_lead_status(lead_id, status):
    """Atualiza apenas o status de um lead."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE leads_chatbot SET status = %s WHERE id = %s", (status, lead_id))
            conn.commit()
        print(f"ℹ️  [DB] Status do Lead {lead_id} atualizado para: {status}")
    except Exception as e:
        print(f"❌ ERRO [DB] ao atualizar status do lead {lead_id}: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

def append_to_chat_history(lead_id, role, text):
    """Adiciona uma mensagem ao histórico JSONB."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            new_message = json.dumps({"role": role, "text": text})
            cur.execute("""
                UPDATE leads_chatbot
                SET historico_chat = 
                    CASE
                        WHEN historico_chat IS NULL THEN '[]'::jsonb
                        ELSE historico_chat
                    END || %s::jsonb
                WHERE id = %s
            """, (new_message, lead_id))
            conn.commit()
    except Exception as e:
        print(f"❌ ERRO [DB] ao salvar histórico do lead {lead_id}: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

# --- 4. [HELPER] Funções da API PageSpeed ---
# (Sem alterações)

def fetch_full_pagespeed_json(url_to_check, api_key):
    """Função helper que chama a API PageSpeed."""
    print(f"ℹ️  [PageSpeed] Iniciando análise para: {url_to_check}")
    categories = "category=SEO&category=PERFORMANCE&category=BEST_PRACTICES"
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
        except: pass
        return None, f"Erro: A API do Google falhou ({error_details})."
    except Exception as e:
        print(f"❌ ERRO Inesperado [PageSpeed]: {e}")
        return None, "Erro: Não foi possível analisar essa URL."

def extract_failing_audits(report_json):
    """Extrai uma lista de auditorias que falharam (score != 1)."""
    audits = report_json.get('lighthouseResult', {}).get('audits', {})
    failed_audits = []
    
    for audit_key, audit_details in audits.items():
        if audit_details.get('scoreDisplayMode') not in ['informative', 'notApplicable'] and audit_details.get('score') is not None and audit_details.get('score') < 0.9:
            failed_audits.append({
                "title": audit_details.get('title'),
                "description": audit_details.get('description'),
                "score": audit_details.get('score')
            })
    print(f"ℹ️  [Parser] Extraídas {len(failed_audits)} auditorias com falha.")
    return failed_audits

# --- 5. [HELPER] Geração de Resposta da IA (GEMINI v1.2) ---
# (Mantida a lógica de generate_content com o modelo global 'gemini-pro')

def generate_ai_response(lead_data, user_message, failed_audits=None):
    """
    Função central que decide qual prompt usar (Qualificação ou Isca-Mestre).
    """
    status = lead_data.get('status')
    
    # --- Lógica de Prompts (inalterada) ---
    if status == 'Coletando Dados':
        missing_data = []
        if not lead_data.get('nome'): missing_data.append("nome (nome do cliente)")
        elif not lead_data.get('email'): missing_data.append("email (email profissional)")
        elif not lead_data.get('whatsapp'): missing_data.append("whatsapp (número com DDD)")
        elif not lead_data.get('cargo'): missing_data.append("cargo (ex: Diretor, Marketing, Dono)")

        if not missing_data:
            return {'next_step': 'generate_isca'}

        system_prompt = f"""
        Você é o "Analista de Ouro", um especialista em SEO.
        Sua missão é coletar os dados que faltam do lead.
        O score de SEO já foi entregue. Você está no meio da conversa.
        REGRAS:
        1.  **Tom de Voz:** Profissional, prestativo e direto.
        2.  **Foco:** Peça APENAS UM DADO FALTANTE por vez.
        3.  **Dados Faltantes:** A lista de dados que você AINDA PRECISA COLETAR é: {missing_data}
        4.  **Sua Tarefa:** Analise o último chat ('{user_message}') e a lista de dados faltantes.
        5.  Se o usuário respondeu o que você pediu, agradeça (ex: "Perfeito, {lead_data.get('nome', 'cliente')}.") e PEÇA O PRÓXIMO item da lista.
        6.  Se o usuário não respondeu, peça novamente o PRIMEIRO item da lista de faltantes.
        """
        
    elif status == 'Gerando Isca':
        system_prompt = f"""
        Você é o "Analista de Ouro", um especialista sênior em SEO e Vendas.
        Sua missão é gerar a "ISCA-MESTRE" (Análise Padrão Ouro) para o lead.
        Você já coletou todos os dados dele. Agora é a hora da venda consultiva.
        DADOS DO LEAD:
        - Nome: {lead_data.get('nome')}
        - Site: {lead_data.get('url_analisada')}
        - Score SEO: {lead_data.get('score_seo')}
        - Cargo: {lead_data.get('cargo')}
        - Falhas Técnicas Detectadas: {json.dumps(failed_audits, ensure_ascii=False)}
        NOSSOS PRODUTOS (FLUXO DE OURO):
        1.  **Base de Ouro (Site/SEO):** Corrigimos falhas técnicas de SEO (como as detectadas), otimizamos o LCP/TTI (velocidade) e criamos sites focados em conversão.
        2.  **Motor de Ouro (Vendedor AI):** Automatizamos a captura e qualificação de leads 24/7 (como eu, o bot) e nutrimos leads via WhatsApp/Email.
        3.  **Mapa de Ouro (Dashboard ROI):** Criamos dashboards em tempo real que mostram de onde vêm os leads e qual o ROI exato dos anúncios.
        REGRAS PARA A ISCA-MESTRE (OBRIGATÓRIO):
        1.  **Tom de Voz:** Especialista máximo. Use o nome do lead (ex: "Certo, {lead_data.get('nome')}.").
        2.  **Diagnóstico:** Comece validando o score ("Seu score de {lead_data.get('score_seo')}/100 é um bom começo...").
        3.  **Conexão (A VENDA):** Analise as {len(failed_audits)} falhas e CONECTE-AS DIRETAMENTE aos nossos produtos.
        4.  **OBRIGATÓRIO:** O texto DEVE conter a tag [RELATORIO_ENVIADO] no final.
        """
        user_message = "Gere a Isca-Mestre com base nos meus dados e falhas."

    elif status in ['Isca Entregue', 'Coletando Orçamento']:
        system_prompt = f"""
        Você é o "Analista de Ouro". Você acabou de entregar a "Isca-Mestre" (o diagnóstico).
        Sua missão agora é qualificar o interesse do lead nos nossos 3 produtos para um orçamento.
        PRODUTOS:
        1. Base de Ouro (Site/SEO)
        2. Motor de Ouro (Vendedor AI)
        3. Mapa de Ouro (Dashboard ROI)
        REGRAS:
        1.  **Tom de Voz:** Consultor de vendas, prestativo.
        2.  **Foco:** Entenda quais produtos o lead quer e qual o objetivo dele.
        3.  **Se o usuário disse 'Sim' para o orçamento:** Comece perguntando quais produtos mais lhe interessaram (Base, Motor ou Mapa).
        4.  **Se o usuário respondeu quais produtos:** Pergunte qual o objetivo principal dele (Ex: "Entendido. E qual seria o objetivo principal? Gerar mais leads? Automatizar o time?").
        5.  **Se o usuário respondeu o objetivo:** Pergunte a faixa de orçamento (Ex: "Perfeito. Para eu montar a melhor proposta, qual sua faixa de orçamento disponível? (Ex: R$ 600, R$ 2000, Acima de R$ 5000)").
        6.  **Se o usuário deu o orçamento:** Agradeça e finalize. Use a tag [ORCAMENTO_FINALIZADO].
        """
    else:
        return {"response_text": "Houve um erro no meu status. Pode recomeçar, por favor?"}

    # --- Implementação generate_content ---
    try:
        if not model:
            return {"error": "IA não configurada."}
            
        # 1. Cria um modelo local com a instrução de sistema
        chat_model = genai.GenerativeModel(
            model_name=model.model_name,
            system_instruction=system_prompt
        )

        # 2. Cria o histórico
        history = [{'role': 'user', 'parts': [{'text': user_message}]}]

        # 3. Chama 'generate_content'
        response = chat_model.generate_content(
            history,
            generation_config=genai.types.GenerationConfig(temperature=0.4),
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE', 'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'}
        )
        
        print(f"🤖 [Gemini] Resposta gerada (Status: {status}): {response.text[:100]}...")
        return {"response_text": response.text}

    except Exception as e:
        print(f"❌ ERRO Inesperado [Gemini] em generate_ai_response (v1.2): {e}")
        traceback.print_exc()
        return {"error": "Desculpe, tive um problema ao processar sua solicitação."}


# --- 6. Endpoint Principal: /api/chat ---
# (Sem alterações)
@app.route('/api/chat', methods=['POST'])
def chat_handler():
# ... (restante da função chat_handler)
    print("\n--- Recebido trigger para /api/chat ---")
    data = request.get_json()
    user_message = data.get('message')
    lead_id = data.get('lead_id') 

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados."}), 500

    lead_data = None
    
    try:
        # -----------------------------------------------------------
        # ESTÁGIO 1: PRIMEIRA MENSAGEM (URL)
        # -----------------------------------------------------------
        if not lead_id:
            
            is_url_like = "." in user_message and len(user_message) > 4 
            
            if not is_url_like:
                print(f"ℹ️  [Fluxo] Novo Lead. Mensagem não é URL: {user_message}")
                bot_response = "Olá! Para começar, preciso que você **cole a URL completa** do seu site (ex: `https://seusite.com.br`) para eu poder analisar."
                return jsonify({"message": bot_response, "lead_id": None}), 200
            
            print(f"ℹ️  [Fluxo] Novo Lead. Mensagem (URL): {user_message}")

            url_analisada = user_message
            if not url_analisada.startswith('http://') and not url_analisada.startswith('https://'):
                url_analisada = 'https://' + url_analisada
                print(f"ℹ️  [Fluxo] URL normalizada para: {url_analisada}")

            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO leads_chatbot (url_analisada, status, historico_chat) VALUES (%s, %s, %s) RETURNING *",
                    (url_analisada, 'Coletando URL', json.dumps([{"role": "user", "text": user_message}]))
                )
                lead_data = cur.fetchone()
                conn.commit()
                lead_id = lead_data['id']
            print(f"✅  [DB] Lead {lead_id} criado (Status: Coletando URL).")

            report_json, error = fetch_full_pagespeed_json(url_analisada, PAGESPEED_API_KEY)
            
            if error:
                print(f"❌ ERRO [PageSpeed] para Lead {lead_id}: {error}")
                update_lead_status(lead_id, 'Erro PageSpeed')
                append_to_chat_history(lead_id, 'bot', error)
                return jsonify({"message": error, "lead_id": lead_id}), 200

            score_seo = (report_json.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score', 0)) * 100
            
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE leads_chatbot SET score_seo = %s, status = 'Coletando Dados' WHERE id = %s",
                    (score_seo, lead_id)
                )
                conn.commit()
            print(f"✅  [DB] Lead {lead_id} atualizado (Score: {score_seo}, Status: Coletando Dados).")

            bot_response = f"🚀 Análise rápida concluída! Seu score de SEO mobile é **{score_seo:.0f}/100**. Detectei algumas falhas que podemos corrigir.\n\nPara começar, qual o seu nome?"
            append_to_chat_history(lead_id, 'bot', bot_response)
            return jsonify({"message": bot_response, "lead_id": lead_id})

        # -----------------------------------------------------------
        # ESTÁGIO 2: CONVERSA EM ANDAMENTO
        # -----------------------------------------------------------
        print(f"ℹ️  [Fluxo] Lead existente: {lead_id}. Mensagem: {user_message}")
        
        append_to_chat_history(lead_id, 'user', user_message)
        
        lead_data = get_lead_by_id(lead_id)
        if not lead_data:
            return jsonify({"error": "Lead não encontrado."}), 404
        
        current_status = lead_data['status']
        print(f"ℹ️  [Fluxo] Status atual do Lead {lead_id}: {current_status}")

        # --- Ação: Qualificação (Coletando Dados) ---
        if current_status == 'Coletando Dados':
            
            missing_data_field = None
            if not lead_data.get('nome'): missing_data_field = "nome"
            elif not lead_data.get('email'): missing_data_field = "email"
            elif not lead_data.get('whatsapp'): missing_data_field = "whatsapp"
            elif not lead_data.get('cargo'): missing_data_field = "cargo"
            
            if missing_data_field:
                with conn.cursor() as cur:
                    cur.execute(f"UPDATE leads_chatbot SET {missing_data_field} = %s WHERE id = %s", (user_message, lead_id))
                    conn.commit()
                print(f"✅  [DB] Lead {lead_id} atualizado. ({missing_data_field} = {user_message})")
                lead_data[missing_data_field] = user_message

            ai_result = generate_ai_response(lead_data, user_message)
            
            if ai_result.get('error'):
                return jsonify({"error": ai_result['error']}), 500
            
            if ai_result.get('next_step') == 'generate_isca':
                print(f"ℹ️  [Fluxo] Coleta de dados do Lead {lead_id} concluída. Mudando status para 'Gerando Isca'.")
                update_lead_status(lead_id, 'Gerando Isca')
                lead_data['status'] = 'Gerando Isca' 
                
                report_json, error = fetch_full_pagespeed_json(lead_data['url_analisada'], PAGESPEED_API_KEY)
                if error:
                    return jsonify({"error": "Não consegui re-analisar seu site para o relatório final."}), 500
                
                failed_audits = extract_failing_audits(report_json)
                
                ai_result = generate_ai_response(lead_data, "N/A", failed_audits)
                if ai_result.get('error'):
                    return jsonify({"error": ai_result['error']}), 500

                bot_response = ai_result['response_text']
                
                if "[RELATORIO_ENVIADO]" in bot_response:
                    parts = bot_response.split("[RELATORIO_ENVIADO]")
                    isca_completa = parts[0].strip()
                    chat_response = parts[1].strip() if len(parts) > 1 else "Relatório enviado. Gostaria de um orçamento?"
                    
                    with conn.cursor() as cur:
                        cur.execute("UPDATE leads_chatbot SET isca = %s, status = 'Isca Entregue' WHERE id = %s", (isca_completa, lead_id))
                        conn.commit()
                    print(f"✅  [DB] Isca-Mestre salva para Lead {lead_id}. Status: 'Isca Entregue'.")
                    
                    append_to_chat_history(lead_id, 'bot', isca_completa + "\n\n" + chat_response)
                    return jsonify({"message": isca_completa + "\n\n" + chat_response, "lead_id": lead_id})
                else:
                    append_to_chat_history(lead_id, 'bot', bot_response)
                    return jsonify({"message": bot_response, "lead_id": lead_id})

            else:
                bot_response = ai_result['response_text']
                append_to_chat_history(lead_id, 'bot', bot_response)
                return jsonify({"message": bot_response, "lead_id": lead_id})

        # --- Ação: Upsell (Coletando Orçamento) ---
        elif current_status in ['Isca Entregue', 'Coletando Orçamento']:
            update_lead_status(lead_id, 'Coletando Orçamento') 
            
            ai_result = generate_ai_response(lead_data, user_message)
            if ai_result.get('error'):
                return jsonify({"error": ai_result['error']}), 500
            
            bot_response = ai_result['response_text']

            if "[ORCAMENTO_FINALIZADO]" in bot_response:
                final_response = bot_response.replace("[ORCAMENTO_FINALIZADO]", "").strip()
                update_lead_status(lead_id, 'Orçamento Coletado')
                append_to_chat_history(lead_id, 'bot', final_response)
                
                if SALES_WEBHOOK_URL:
                    try:
                        lead_data_full = get_lead_by_id(lead_id) 
                        
                        payload = {
                            "lead_info": lead_data_full,
                            "orcamento_info": " (Dados do orçamento aqui) "
                        }
                        requests.post(SALES_WEBHOOK_URL, json=payload, timeout=5)
                        print(f"✅  [Webhook] Webhook de Vendas disparado para Lead {lead_id}.")
                    except Exception as e:
                        print(f"⚠️ AVISO [Webhook] Falha ao disparar webhook de vendas: {e}")
                        
                return jsonify({"message": final_response, "lead_id": lead_id})
            
            append_to_chat_history(lead_id, 'bot', bot_response)
            return jsonify({"message": bot_response, "lead_id": lead_id})
            
        else:
            print(f"⚠️ AVISO [Fluxo] Lead {lead_id} em status desconhecido: {current_status}")
            return jsonify({"message": "Estou reiniciando meu fluxo, um momento...", "lead_id": lead_id})

    except Exception as e:
        print(f"❌ ERRO Fatal [Fluxo] em /api/chat: {e}")
        traceback.print_exc()
        if conn: conn.rollback()
        return jsonify({"error": "Ocorreu um erro fatal no processamento do chat."}), 500
    finally:
        if conn:
            conn.close()
            print("🔌  [DB] Conexão principal do /api/chat fechada.")


# --- Endpoint de Teste (NOVO) ---
@app.route('/api/test-gemini', methods=['GET'])
def test_gemini_models():
    """
    Tenta usar vários modelos conhecidos que podem funcionar na API v1beta
    e retorna o primeiro que conseguir gerar uma resposta.
    """
    if not GEMINI_API_KEY:
        return jsonify({"status": "error", "message": "GEMINI_API_KEY não configurada."}), 500

    TEST_MODELS = [
        "gemini-pro",
        "gemini-1.0-pro",
        "gemini-1.0-flash",
        "gemini-1.5-flash-latest",
        "gemini-1.5-flash"
    ]

    for model_name in TEST_MODELS:
        try:
            print(f"ℹ️  [TESTE] Tentando modelo: {model_name}")
            
            # 1. Cria o modelo
            test_model = genai.GenerativeModel(model_name)
            
            # 2. Gera um conteúdo simples (o método generate_content é o correto)
            response = test_model.generate_content("Responda apenas 'OK'.")
            
            if response.text and response.text.strip().upper() == 'OK':
                print(f"✅  [SUCESSO] Modelo aceito: {model_name}")
                return jsonify({
                    "status": "success", 
                    "accepted_model": model_name,
                    "message": f"O ambiente aceita {model_name}. Use este modelo no app.py."
                }), 200
                
        except exceptions.NotFound:
            print(f"❌ [FALHA] Modelo {model_name} não encontrado (404/v1beta).")
            continue
        except Exception as e:
            print(f"⚠️ [ERRO GERAL] Falha ao testar {model_name}: {e}")
            continue

    return jsonify({
        "status": "fail", 
        "message": "Nenhum modelo popular foi aceito pela API v1beta. O ambiente está severamente desatualizado."
    }), 200


# --- Endpoint 7: Webhook para N8N (Atualizar Status) ---
# (Sem alterações)
@app.route('/api/update-status-n8n', methods=['POST'])
def update_status_n8n():
    # ... (restante da função update_status_n8n)
    print("\n--- Recebido trigger para /api/update-status-n8n ---")
    
    auth_header = request.headers.get('Authorization')
    secret_key = auth_header.split(' ')[1] if auth_header and 'Bearer' in auth_header else None
    
    if not secret_key or secret_key != N8N_SECRET_KEY:
        print("❌ ERRO [Auth] Tentativa de acesso não autorizada ao /api/update-status-n8n.")
        return jsonify({"error": "Não autorizado"}), 401
        
    data = request.get_json()
    lead_id = data.get('lead_id')
    new_status = data.get('new_status')
    email_enviado_flag = data.get('email_enviado', False) 

    if not lead_id or not new_status:
        return jsonify({"error": "lead_id e new_status são obrigatórios."}), 400

    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de DB"}), 500
    
    try:
        with conn.cursor() as cur:
            print(f"ℹ️  [DB-N8N] Atualizando Lead ID: {lead_id} para '{new_status}'...")
            cur.execute("""
                UPDATE leads_chatbot 
                SET status = %s, email_enviado = %s
                WHERE id = %s
            """, (new_status, email_enviado_flag, lead_id))
            conn.commit()
            
        print("✅  [DB-N8N] Status atualizado com sucesso.")
        return jsonify({"success": True, "lead_id": lead_id, "new_status": new_status}), 200

    except Exception as e:
        print(f"❌ ERRO [DB-N8N] ao atualizar o status: {e}")
        traceback.print_exc()
        if conn: conn.rollback()
        return jsonify({"error": f"Erro ao atualizar o status: {e}"}), 500
    finally:
        if conn: conn.close()

# --- Endpoint 8: Diagnóstico Rápido (Barra de Busca) ---
# (Sem alterações)
@app.route('/api/get-pagespeed', methods=['POST'])
def get_pagespeed_report():
    # ... (restante da função get_pagespeed_report)
    print("\n--- Recebido trigger para /api/get-pagespeed (Barra de Busca) ---")
    
    if not PAGESPEED_API_KEY:
        return jsonify({"status_message": "Erro: O servidor não está configurado."}), 500
    
    inspected_url = request.get_json().get('inspected_url')
    if not inspected_url:
        return jsonify({"status_message": "Erro: Nenhuma URL fornecida."}), 400
    
    if not inspected_url.startswith('http://') and not inspected_url.startswith('https://'):
        inspected_url = 'https://' + inspected_url

    results, error = fetch_full_pagespeed_json(inspected_url, PAGESPEED_API_KEY)
    
    if error:
        return jsonify({"status_message": error}), 502

    seo_score_raw = results.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score')
    
    if seo_score_raw is None:
         return jsonify({"status_message": "Erro: Não foi possível extrair o score."}), 500

    seo_score = seo_score_raw * 100
    status_message = f"Diagnóstico Mobile: 🚀 SEO: {seo_score:.0f}/100."
    
    return jsonify({"status_message": status_message}), 200


# --- Execução do App ---
if __name__ == "__main__":
    # setup_database() # <-- Descomente se precisar criar as tabelas
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
