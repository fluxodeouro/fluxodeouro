import os
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

# Carrega variáveis do .env (APENAS para testes locais)
load_dotenv() 

app = Flask(__name__)
CORS(app) 

# --- Configuração Lida do Ambiente (Render) ---
# O Render irá preencher esta variável a partir do painel [cite: image_e13af9.png]
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY")

# [OPCIONAL] O script não usa o DB, mas se você quiser conectar
# para OUTRA funcionalidade, pode deixar.
DATABASE_URL = os.environ.get("DATABASE_URL")

@app.route('/')
def index():
    return jsonify({"message": "Fluxo de Ouro API Service (PageSpeed) is running"})

# --- ENDPOINT (PageSpeed Insights) ---
@app.route('/api/get-pagespeed', methods=['POST'])
def get_pagespeed_report():
    """Endpoint para analisar o SEO de uma URL pública via PageSpeed."""
    print("\n--- Recebido trigger para API PageSpeed Insights ---")
    
    # 1. Valida se a chave de API foi carregada do ambiente
    if not PAGESPEED_API_KEY:
        print("❌ ERRO Fatal: PAGESPEED_API_KEY não definida.")
        return jsonify({"status_message": "Erro: O servidor não está configurado."}), 500
    
    # 2. Pega a URL da requisição
    try:
        data = request.get_json()
        inspected_url = data.get('inspected_url')
        if not inspected_url:
            return jsonify({"status_message": "Erro: Nenhuma URL fornecida."}), 400
    except Exception as e:
        return jsonify({"status_message": f"Erro: Requisição mal formatada. {e}"}), 400

    print(f"Analisando URL do usuário: {inspected_url}")

    # 3. Constrói a URL da API e faz a chamada
    try:
        # Foca em SEO e Mobile
        api_url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url={inspected_url}&key={PAGESPEED_API_KEY}&category=SEO&strategy=MOBILE"
        
        response = requests.get(api_url, timeout=30) 
        response.raise_for_status() 
        results = response.json()
        
        # 4. Extrai o score de SEO
        seo_score_raw = results.get('lighthouseResult', {}).get('categories', {}).get('seo', {}).get('score')
        
        if seo_score_raw is None:
             print("❌ ERRO: Resposta da API não continha 'score' de SEO.")
             return jsonify({"status_message": "Erro: Não foi possível extrair o score."}), 500

        seo_score = seo_score_raw * 100
        status_message = f"Diagnóstico Mobile: 🚀 SEO: {seo_score:.0f}/100."
        
        print(f"✅ Análise PageSpeed concluída: {status_message}")
        return jsonify({"status_message": status_message}), 200

    except requests.exceptions.HTTPError as http_err:
         print(f"❌ ERRO HTTP PageSpeed: {http_err}")
         error_details = "Erro desconhecido"
         try:
             error_details = http_err.response.json().get('error', {}).get('message', 'Verifique a URL')
         except:
             pass
         return jsonify({"status_message": f"Erro: A API falhou ({error_details})."}), 502
    except Exception as e:
        print(f"❌ ERRO Inesperado PageSpeed: {e}")
        return jsonify({"status_message": "Erro: Não foi possível analisar essa URL."}), 500

# --- Execução do App (Pronto para Render/Gunicorn) ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
