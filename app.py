# Fluxo de Ouro API Service (v6.0 - Vendedor-Consultor IA)
# Este app implementa o fluxo completo de captura, diagnóstico,
# qualificação, geração de isca (Padrão Ouro) e upsell (orçamento).
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
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash-latest') # Usando 1.5-flash para prompts longos
        print("✅  [Gemini] Modelo ('gemini-1.5-flash-latest') inicializado.")
    else:
        model = None
        print("❌ ERRO: GEMINI_API_KEY não encontrada. O Chatbot não funcionará.")
except Exception as e:
    model = None
    print(f"❌ Erro ao configurar a API do Gemini: {e}")
    traceback.print_exc()

# --- 3. [HELPER] Funções do Banco de Dados ---

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
        # Pega apenas falhas (score < 0.9) que não sejam apenas informativas
        if audit_details.get('scoreDisplayMode') not in ['informative', 'notApplicable'] and audit_details.get('score') is not None and audit_details.get('score') < 0.9:
            failed_audits.append({
                "title": audit_details.get('title'),
                "description": audit_details.get('description'),
                "score": audit_details.get('score')
            })
    print(f"ℹ️  [Parser] Extraídas {len(failed_audits)} auditorias com falha.")
    return failed_audits

# --- 5. [HELPER] Geração de Resposta da IA (Gemini) ---

def generate_ai_response(lead_data, user_message, failed_audits=None):
    """
    Função central que decide qual prompt usar (Qualificação ou Isca-Mestre).
    """
    status = lead_data.get('status')
    
    # -----------------------------------------------------------
    # PROMPT 1: QUALIFICAÇÃO (Coletando Dados)
    # -----------------------------------------------------------
    if status == 'Coletando Dados':
        # Monta a lista de dados que FALTAM
        missing_data = []
        if not lead_data.get('nome'): missing_data.append("nome (nome do cliente)")
        if not lead_data.get('email'): missing_data.append("email (email profissional)")
        if not lead_data.get('whatsapp'): missing_data.append("whatsapp (número com DDD)")
        if not lead_data.get('cargo'): missing_data.append("cargo (ex: Diretor, Marketing, Dono)")

        if not missing_data:
            # Se não falta nada, é hora de gerar a isca!
            # (Este é o gatilho para o próximo estágio)
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

        EXEMPLO DE RESPOSTA (se o próximo item for 'email'):
        "Obrigado, {lead_data.get('nome', 'cliente')}! 
        Para qual e-mail profissional posso enviar a análise completa?"
        
        EXEMPLO DE RESPOSTA (se o próximo item for 'whatsapp'):
        "Entendido. E qual o seu WhatsApp com DDD? Usamos ele para agendar a consultoria de 15 minutos."
        """
        
    # -----------------------------------------------------------
    # PROMPT 2: ISCA-MESTRE (Padrão Ouro)
    # -----------------------------------------------------------
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

        EXEMPLO DE RESPOSTA PERFEITA:
        "Certo, {lead_data.get('nome')}. Análise concluída.
        
        Seu score de {lead_data.get('score_seo')}/100 é um bom começo, mas identifiquei {len(failed_audits)} falhas técnicas críticas.
        
        Por exemplo, vi que seu site tem problemas de velocidade (LCP lento) e falhas de indexação. Isso significa que, mesmo que seu site seja bonito, os clientes e o Google não o encontram ou desistem antes de carregar.
        
        É exatamente aqui que a **Base de Ouro (nosso serviço de SEO/Site)** entra, corrigindo essas falhas para transformar visitantes em clientes.
        
        Também notei que seu site não possui um sistema de captura ativo. Você está perdendo leads que saem da página.
        Nosso **Motor de Ouro (Vendedor AI)** poderia estar capturando e qualificando esses leads para você 24/7.
        
        Enviei o relatório técnico completo para o seu e-mail ({lead_data.get('email')}).
        [RELATORIO_ENVIADO]
        
        Baseado no seu cargo de {lead_data.get('cargo')}, sei que seu foco é em resultados. Você gostaria de iniciar um orçamento para um plano de ação?"
        """
        user_message = "Gere a Isca-Mestre com base nos meus dados e falhas."

    # -----------------------------------------------------------
    # PROMPT 3: UPSELL (Coletando Orçamento)
    # -----------------------------------------------------------
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
        # Fallback
        return {"response_text": "Houve um erro no meu status. Pode recomeçar, por favor?"}

    # --- Execução do Gemini ---
    try:
        if not model:
            return {"error": "IA não configurada."}
            
        chat_session = model.start_chat(history=[])
        full_prompt = f"{system_prompt}\n\nÚltima mensagem do usuário: {user_message}"
        
        response = chat_session.send_message(
            full_prompt,
            generation_config=genai.types.GenerationConfig(temperature=0.4),
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE', 'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'}
        )
        
        print(f"🤖 [Gemini] Resposta gerada (Status: {status}): {response.text[:100]}...")
        return {"response_text": response.text}

    except Exception as e:
        print(f"❌ ERRO Inesperado [Gemini] em generate_ai_response: {e}")
        traceback.print_exc()
        return {"error": "Desculpe, tive um problema ao processar sua solicitação."}


# --- 6. Endpoint Principal: /api/chat ---
@app.route('/api/chat', methods=['POST'])
def chat_handler():
    """
    Endpoint ÚNICO para gerenciar todo o fluxo do chatbot.
    Gerencia o estado do lead (Coleta de URL, Coleta de Dados, Geração de Isca, Orçamento).
    """
    print("\n--- Recebido trigger para /api/chat ---")
    data = request.get_json()
    user_message = data.get('message')
    lead_id = data.get('lead_id') # Pode ser nulo na primeira mensagem

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados."}), 500

    lead_data = None
    
    try:
        # -----------------------------------------------------------
        # ESTÁGIO 1: PRIMEIRA MENSAGEM (URL)
        # -----------------------------------------------------------
        if not lead_id:
            print(f"ℹ️  [Fluxo] Novo Lead. Mensagem (URL): {user_message}")
            url_analisada = user_message # A primeira mensagem é a URL
            
            # --- Ação: Salva Imediatamente ---
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO leads_chatbot (url_analisada, status, historico_chat) VALUES (%s, %s, %s) RETURNING *",
                    (url_analisada, 'Coletando URL', json.dumps([{"role": "user", "text": url_analisada}]))
                )
                lead_data = cur.fetchone()
                conn.commit()
                lead_id = lead_data['id']
            print(f"✅  [DB] Lead {lead_id} criado (Status: Coletando URL).")

            # --- Ação: Busca PageSpeed ---
            report_json, error = fetch_full_pagespeed_json(url_analisada, PAGESPEED_API_KEY)
            if error:
                print(f"❌ ERRO [PageSpeed] para Lead {lead_id}: {error}")
                # Atualiza o status de erro e informa o usuário
                update_lead_status(lead_id, 'Erro PageSpeed')
                append_to_chat_history(lead_id, 'bot', error)
                return jsonify({"message": error, "lead_id": lead_id}), 200

            score_seo = (report_json.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score', 0)) * 100
            
            # --- Ação: Atualiza o Lead com o Score e muda o Status ---
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE leads_chatbot SET score_seo = %s, status = 'Coletando Dados' WHERE id = %s",
                    (score_seo, lead_id)
                )
                conn.commit()
            print(f"✅  [DB] Lead {lead_id} atualizado (Score: {score_seo}, Status: Coletando Dados).")

            # Prepara a primeira resposta (iniciando a qualificação)
            bot_response = f"🚀 Análise rápida concluída! Seu score de SEO mobile é **{score_seo:.0f}/100**. Detectei algumas falhas que podemos corrigir.\n\nPara começar, qual o seu nome?"
            append_to_chat_history(lead_id, 'bot', bot_response)
            return jsonify({"message": bot_response, "lead_id": lead_id})

        # -----------------------------------------------------------
        # ESTÁGIO 2: CONVERSA EM ANDAMENTO
        # -----------------------------------------------------------
        print(f"ℹ️  [Fluxo] Lead existente: {lead_id}. Mensagem: {user_message}")
        
        # Salva a mensagem do usuário
        append_to_chat_history(lead_id, 'user', user_message)
        
        # Busca os dados atuais do lead
        lead_data = get_lead_by_id(lead_id)
        if not lead_data:
            return jsonify({"error": "Lead não encontrado."}), 404
        
        current_status = lead_data['status']
        print(f"ℹ️  [Fluxo] Status atual do Lead {lead_id}: {current_status}")

        # --- Ação: Qualificação (Coletando Dados) ---
        if current_status == 'Coletando Dados':
            # Tenta salvar o dado que o usuário acabou de enviar
            
            # 1. Descobre o que estava faltando
            missing_data_field = None
            if not lead_data.get('nome'): missing_data_field = "nome"
            elif not lead_data.get('email'): missing_data_field = "email"
            elif not lead_data.get('whatsapp'): missing_data_field = "whatsapp"
            elif not lead_data.get('cargo'): missing_data_field = "cargo"
            
            # 2. Salva o dado
            if missing_data_field:
                with conn.cursor() as cur:
                    # Cuidado: SQL Injection (simples, mas ok para este caso)
                    cur.execute(f"UPDATE leads_chatbot SET {missing_data_field} = %s WHERE id = %s", (user_message, lead_id))
                    conn.commit()
                print(f"✅  [DB] Lead {lead_id} atualizado. ({missing_data_field} = {user_message})")
                # Atualiza os dados locais para o Gemini
                lead_data[missing_data_field] = user_message

            # 3. Chama a IA para pedir o próximo dado
            ai_result = generate_ai_response(lead_data, user_message)
            
            if ai_result.get('error'):
                return jsonify({"error": ai_result['error']}), 500
            
            # 4. Verifica se a coleta ACABOU
            if ai_result.get('next_step') == 'generate_isca':
                print(f"ℹ️  [Fluxo] Coleta de dados do Lead {lead_id} concluída. Mudando status para 'Gerando Isca'.")
                update_lead_status(lead_id, 'Gerando Isca')
                lead_data['status'] = 'Gerando Isca' # Atualiza o status local
                
                # --- Ação: Gerar a Isca-Mestre (IMEDIATAMENTE) ---
                
                # (Re)Busca os dados do PageSpeed para o prompt da Isca-Mestre
                report_json, error = fetch_full_pagespeed_json(lead_data['url_analisada'], PAGESPEED_API_KEY)
                if error:
                    return jsonify({"error": "Não consegui re-analisar seu site para o relatório final."}), 500
                
                failed_audits = extract_failing_audits(report_json)
                
                ai_result = generate_ai_response(lead_data, "N/A", failed_audits)
                if ai_result.get('error'):
                    return jsonify({"error": ai_result['error']}), 500

                bot_response = ai_result['response_text']
                
                # Extrai a isca e o que vai pro chat
                if "[RELATORIO_ENVIADO]" in bot_response:
                    parts = bot_response.split("[RELATORIO_ENVIADO]")
                    isca_completa = parts[0].strip()
                    chat_response = parts[1].strip() if len(parts) > 1 else "Relatório enviado. Gostaria de um orçamento?"
                    
                    # Salva a Isca no DB
                    with conn.cursor() as cur:
                        cur.execute("UPDATE leads_chatbot SET isca = %s, status = 'Isca Entregue' WHERE id = %s", (isca_completa, lead_id))
                        conn.commit()
                    print(f"✅  [DB] Isca-Mestre salva para Lead {lead_id}. Status: 'Isca Entregue'.")
                    
                    # Salva o histórico e retorna
                    append_to_chat_history(lead_id, 'bot', isca_completa + "\n\n" + chat_response)
                    return jsonify({"message": isca_completa + "\n\n" + chat_response, "lead_id": lead_id})
                else:
                    # Fallback caso a IA não use a tag
                    append_to_chat_history(lead_id, 'bot', bot_response)
                    return jsonify({"message": bot_response, "lead_id": lead_id})

            else:
                # Continua a coleta
                bot_response = ai_result['response_text']
                append_to_chat_history(lead_id, 'bot', bot_response)
                return jsonify({"message": bot_response, "lead_id": lead_id})

        # --- Ação: Upsell (Coletando Orçamento) ---
        elif current_status in ['Isca Entregue', 'Coletando Orçamento']:
            update_lead_status(lead_id, 'Coletando Orçamento') # Garante o status
            
            ai_result = generate_ai_response(lead_data, user_message)
            if ai_result.get('error'):
                return jsonify({"error": ai_result['error']}), 500
            
            bot_response = ai_result['response_text']

            # TODO: Aqui entraria a lógica para PARSEAR a resposta do usuário
            # e salvar os dados (interesse_base_ouro, etc.) na tabela 'orcar_chatbot'.
            # Por enquanto, apenas continuamos a conversa.

            # Se a IA finalizou o orçamento
            if "[ORCAMENTO_FINALIZADO]" in bot_response:
                final_response = bot_response.replace("[ORCAMENTO_FINALIZADO]", "").strip()
                update_lead_status(lead_id, 'Orçamento Coletado')
                append_to_chat_history(lead_id, 'bot', final_response)
                
                # --- Ação: Dispara o Webhook de Vendas ---
                if SALES_WEBHOOK_URL:
                    try:
                        # Busca o orçamento salvo (que ainda não foi implementado)
                        # e os dados do lead
                        lead_data_full = get_lead_by_id(lead_id) 
                        # Aqui você buscaria os dados do 'orcar_chatbot' também
                        
                        payload = {
                            "lead_info": lead_data_full,
                            "orcamento_info": " (Dados do orçamento aqui) "
                        }
                        # Dispara em modo "não-bloqueante"
                        requests.post(SALES_WEBHOOK_URL, json=payload, timeout=5)
                        print(f"✅  [Webhook] Webhook de Vendas disparado para Lead {lead_id}.")
                    except Exception as e:
                        print(f"⚠️ AVISO [Webhook] Falha ao disparar webhook de vendas: {e}")
                        
                return jsonify({"message": final_response, "lead_id": lead_id})
            
            # Continua a coleta do orçamento
            append_to_chat_history(lead_id, 'bot', bot_response)
            return jsonify({"message": bot_response, "lead_id": lead_id})
            
        else:
            # Status desconhecido
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


# --- Endpoint 7: Webhook para N8N (Atualizar Status) ---
@app.route('/api/update-status-n8n', methods=['POST'])
def update_status_n8n():
    """
    Webhook SEGURO para o N8N (ou outro workflow) atualizar o status
    de um lead. (Ex: 'Email Enviado').
    """
    print("\n--- Recebido trigger para /api/update-status-n8n ---")
    
    # 1. Verifica a Chave Secreta
    auth_header = request.headers.get('Authorization')
    secret_key = auth_header.split(' ')[1] if auth_header and 'Bearer' in auth_header else None
    
    if not secret_key or secret_key != N8N_SECRET_KEY:
        print("❌ ERRO [Auth] Tentativa de acesso não autorizada ao /api/update-status-n8n.")
        return jsonify({"error": "Não autorizado"}), 401
        
    data = request.get_json()
    lead_id = data.get('lead_id')
    new_status = data.get('new_status')
    email_enviado_flag = data.get('email_enviado', False) # Opcional

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
@app.route('/api/get-pagespeed', methods=['POST'])
def get_pagespeed_report():
    """Endpoint para o diagnóstico rápido da barra de busca do index.html."""
    print("\n--- Recebido trigger para /api/get-pagespeed (Barra de Busca) ---")
    
    if not PAGESPEED_API_KEY:
        return jsonify({"status_message": "Erro: O servidor não está configurado."}), 500
    
    inspected_url = request.get_json().get('inspected_url')
    if not inspected_url:
        return jsonify({"status_message": "Erro: Nenhuma URL fornecida."}), 400

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
    # O 'setup_database' (que você tinha no app.py anterior)
    # agora é executado pelos scripts do Colab,
    # então não precisamos mais dele aqui.
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
