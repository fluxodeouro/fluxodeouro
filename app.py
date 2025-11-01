import os
import requests
import json
import google.generativeai as genai # <- Adicionado do Taurusbot [cite: app - Copia.py]
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

# Carrega variáveis do .env (APENAS para testes locais)
load_dotenv() 

app = Flask(__name__)
CORS(app) 

# --- 1. Configuração Lida do Ambiente (Render) ---
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL") # Mantido para o futuro "Mapa de Ouro"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") # <- Adicionado do Taurusbot [cite: app - Copia.py]

# --- 2. Configuração do Gemini (do Taurusbot) ---
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

# --- 3. [HELPER] Função de Análise PageSpeed (Retorna o JSON completo) ---
def fetch_full_pagespeed_json(url_to_check, api_key):
    """
    Função helper que chama a API PageSpeed e retorna o JSON completo.
    Ambos os endpoints usarão isso.
    """
    print(f"ℹ️  [PageSpeed] Iniciando análise para: {url_to_check}")
    
    # Define as categorias que queremos analisar
    categories = "category=SEO&category=PERFORMANCE&category=ACCESSIBILITY&category=BEST_PRACTICES"
    api_url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url={url_to_check}&key={api_key}&{categories}&strategy=MOBILE"
    
    try:
        response = requests.get(api_url, timeout=45) # Timeout maior para análise completa
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

# --- 4. [HELPER] Função para extrair falhas do JSON ---
def extract_failing_audits(report_json):
    """
    Extrai uma lista de auditorias que falharam (score != 1) do relatório JSON.
    """
    audits = report_json.get('lighthouseResult', {}).get('audits', {})
    failed_audits = []
    
    for audit_key, audit_details in audits.items():
        # Consideramos falha se o score não for 1 (perfeito) e se não for apenas "informativo"
        if audit_details.get('scoreDisplayMode') != 'informative' and audit_details.get('score') is not None and audit_details.get('score') < 1:
            failed_audits.append({
                "title": audit_details.get('title'),
                "description": audit_details.get('description'),
                "score": audit_details.get('score')
            })
    print(f"ℹ️  [Parser] Extraídas {len(failed_audits)} auditorias com falha.")
    return failed_audits

# --- 5. Endpoints da API ---

@app.route('/')
def index():
    return jsonify({"message": "Fluxo de Ouro API Service (PageSpeed + Gemini) is running"})

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

    # Usa a função helper para buscar o JSON
    results, error = fetch_full_pagespeed_json(inspected_url, PAGESPEED_API_KEY)
    
    if error:
        return jsonify({"status_message": error}), 502

    # Extrai apenas o score de SEO para a resposta rápida
    seo_score_raw = results.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score')
    
    if seo_score_raw is None:
         print("❌ ERRO: Resposta da API não continha 'score' de SEO.")
         return jsonify({"status_message": "Erro: Não foi possível extrair o score."}), 500

    seo_score = seo_score_raw * 100
    status_message = f"Diagnóstico Mobile: 🚀 SEO: {seo_score:.0f}/100."
    
    print(f"✅ Análise PageSpeed Rápida concluída: {status_message}")
    return jsonify({"status_message": status_message}), 200

# --- Endpoint 2: Chatbot (Diagnóstico com IA) ---
@app.route('/api/get-seo-diagnosis', methods=['POST'])
def get_seo_diagnosis():
    """Endpoint para o diagnóstico profundo do Chatbot com Gemini."""
    print("\n--- Recebido trigger para /api/get-seo-diagnosis ---")
    
    # 1. Valida se AMBAS as chaves estão carregadas
    if not PAGESPEED_API_KEY or not model:
        print("❌ ERRO: PAGESPEED_API_KEY ou GEMINI_API_KEY não definidas.")
        return jsonify({"error": "Erro: O servidor não está configurado para o diagnóstico de IA."}), 500

    # 2. Pega a URL do usuário
    try:
        data = request.get_json()
        user_url = data.get('user_url')
        if not user_url:
            return jsonify({"error": "Nenhuma URL fornecida."}), 400
    except Exception:
        return jsonify({"error": "Requisição mal formatada."}), 400

    # 3. Define o "Padrão Ouro"
    golden_url = "https://teclabel.com.br/"

    try:
        # 4. Busca os relatórios (Usuário e Padrão Ouro)
        user_report, user_error = fetch_full_pagespeed_json(user_url, PAGESPEED_API_KEY)
        golden_report, golden_error = fetch_full_pagespeed_json(golden_url, PAGESPEED_API_KEY)

        if user_error:
            return jsonify({"error": user_error}), 502
        if golden_error:
            # Se o Padrão Ouro falhar, ainda podemos continuar, mas avisamos no log
            print("⚠️ AVISO: Não foi possível buscar o relatório 'Padrão Ouro'. O diagnóstico será parcial.")
            golden_report = {} # Envia um relatório vazio para o Gemini

        # 5. Extrai as falhas do usuário
        user_failing_audits = extract_failing_audits(user_report)
        # Extrai o score geral de SEO do usuário
        user_seo_score = (user_report.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score', 0)) * 100

        # 6. Cria o System Prompt para o Gemini (O "Analista de Ouro")
        system_prompt = f"""
        Você é o "Analista de Ouro", um especialista sênior em SEO e Performance Web.
        Sua missão é dar um diagnóstico claro, direto e acionável para um usuário que enviou a URL do site dele.

        REGRAS:
        1.  **Tom de Voz:** Profissional, especialista, mas encorajador. Use 🚀 e 💡.
        2.  **Referência:** Você vai comparar as falhas do site do usuário com um "Padrão Ouro" (um site nota 100/100) que eu vou fornecer.
        3.  **NÃO CITE O NOME:** NUNCA mencione o nome do site Padrão Ouro (teclabel.com.br). Chame-o apenas de "nosso padrão de referência 100/100".
        4.  **Seja Específico:** Dê 3 a 4 dicas práticas baseadas nas *piores* falhas (menor score) do usuário.
        5.  **Formato:** Use Markdown (negrito, bullet points) para formatar a resposta.
        6.  **Foco:** Foque nas auditorias de SEO, Performance e Acessibilidade.
        7.  **Encerramento:** Sempre termine com um call-to-action para o usuário contratar os serviços da "Fluxo de Ouro" para implementar as melhorias.

        ---
        ANÁLISE DO SITE DO USUÁRIO ({user_url}):
        - Score Geral de SEO: {user_seo_score:.0f}/100
        - Auditorias com Falha: {json.dumps(user_failing_audits, ensure_ascii=False)}
        
        RELATÓRIO DO SITE "PADRÃO OURO" (Nota 100/100):
        - (Relatório completo do Padrão Ouro anexado para sua referência de como é um site perfeito.)
        ---
        
        DIAGNÓSTICO (comece aqui):
        """
        
        # Prepara o chat (similar ao Taurusbot, mas sem histórico longo)
        chat_session = model.start_chat(history=[])
        
        # 7. Envia para o Gemini
        response = chat_session.send_message(
            system_prompt,
            generation_config=genai.types.GenerationConfig(temperature=0.5),
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE',
                             'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'}
        )

        print(f"🤖 [Gemini] Diagnóstico gerado: {response.text[:100]}...")
        return jsonify({'diagnosis': response.text})

    except Exception as e:
        print(f"❌ ERRO Inesperado em /api/get-seo-diagnosis: {e}")
        return jsonify({'error': 'Ocorreu um erro ao gerar o diagnóstico de IA.'}), 500


# --- Execução do App (Pronto para Render/Gunicorn) ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

