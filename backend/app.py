from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
import io
import os
import tempfile
import re

app = Flask(__name__)

# Configuração mais permissiva do CORS
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["OPTIONS", "GET", "POST"],
        "allow_headers": ["Content-Type"]
    }
})

# Aumentar limite de upload para 16MB
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

def processar_nota_corretagem(pdf_path):
    print(pdf_path)
    # Abrir o PDF e extrair o texto
    with pdfplumber.open(pdf_path) as pdf:
        texto = ""
        for page in pdf.pages:
            texto += page.extract_text()
    print(texto)
    # Extrair o número da nota e a data do pregão  
    numero_nota = "0000000"  
    data_pregao = "N/A"  

    # Procurar pelo cabeçalho "Nr. nota Folha Data pregão" e capturar a linha seguinte  
    cabecalho_match = re.search(r"Nr\. nota  Folha  Data pregão\n(\d+)\s+\d+\s+(\d{2}/\d{2}/\d{4})", texto)  
    if cabecalho_match:  
        numero_nota = cabecalho_match.group(1)  # Capturar o número da nota  
        data_pregao = cabecalho_match.group(2)  # Capturar a data do pregão

    # Extrair as operações
    operacoes = []
    tabela_match = re.findall(
        r"1-BOVESPA\s+[CV]\s+VISTA\s+([A-Z0-9]+(?:\s+[A-Z]+)?)\s+(\d+)\s+([\d,.]+)\s+([\d,.]+)\s+([DC])",
        texto
    )    
    for operacao in tabela_match:
        ticker, quantidade, preco, valor, tipo_dc = operacao
        ticker = ticker.split()[0].rstrip("F")  # Remover o sufixo F, se presente
        quantidade = int(quantidade)

        # Tratar valores no formato brasileiro (remover separadores de milhares e ajustar decimais)
        preco = float(re.sub(r"[.]", "", preco).replace(",", "."))
        valor = float(re.sub(r"[.]", "", valor).replace(",", "."))

        # Determinar o tipo de operação com base na coluna "D/C"
        tipo_operacao = "C" if tipo_dc == "D" else "V"
        if tipo_operacao == "V":
            quantidade = -quantidade  # Quantidade negativa para vendas

        # Adicionar a operação à lista
        operacoes.append({
            "ticker": ticker,
            "quantidade": quantidade,
            "preco": preco,
            "valor": valor,
            "tipo": tipo_operacao
        })

    # Identificar operações de venda separadamente
    operacoes_venda = [op for op in operacoes if op["tipo"] == "V"]

    # Calcular as taxas proporcionais (não arredondadas ainda)
    taxa_liquidacao_match = re.search(r"Taxa de liquidação\s+([\d,]+)", texto)
    taxa_liquidacao = float(taxa_liquidacao_match.group(1).replace(",", ".")) if taxa_liquidacao_match else 0.0

    emolumentos_match = re.search(r"Emolumentos\s+([\d,]+)", texto)
    emolumentos = float(emolumentos_match.group(1).replace(",", ".")) if emolumentos_match else 0.0

    total_operacoes = sum(op["valor"] for op in operacoes)
    total_taxas = taxa_liquidacao + emolumentos

    taxa_acumulada = 0.0  # Para verificar a soma real
    for i, op in enumerate(operacoes):
        valor_operacao = op["valor"]
        # Calcular a taxa proporcional não arredondada
        taxa_calculada = (valor_operacao / total_operacoes) * total_taxas if total_operacoes > 0 else 0.0

        # Arredondar para 2 casas decimais ao exibir
        taxas_arredondada = round(taxa_calculada, 2)
        taxa_acumulada += taxas_arredondada

        # Ajustar a última operação para garantir soma exata
        if i == len(operacoes) - 1:
            diferenca_taxa = round(total_taxas - taxa_acumulada, 2)
            taxas_arredondada += diferenca_taxa  # Corrigir última operação

        op["taxa"] = taxas_arredondada  # Aplicar o valor final arredondado

    # Calcular IRRF proporcional
    irrf_match = re.search(r"I\.R\.R\.F\.\s+s/ operações, base R\$ ([\d,.]+)\s+([\d,.]+)", texto)
    if irrf_match:
        # Corrigir valores extraídos, removendo pontos de milhar e ajustando vírgula decimal
        base_irrf = float(re.sub(r"[.]", "", irrf_match.group(1)).replace(",", "."))
        irrf_total = float(irrf_match.group(2).replace(",", "."))
    else:
        base_irrf = 0.0
        irrf_total = 0.0

    irrf_acumulado = 0.0
    # Acompanhar o índice dentro de `operacoes_venda`
    for i, op in enumerate(operacoes_venda):  # Iterar apenas sobre operações de venda
        # Calcular o IRRF proporcional com base no valor da operação de venda
        valor_operacao = op["valor"]
        irrf_calculado = (valor_operacao / base_irrf) * irrf_total if base_irrf > 0 else 0.0

        # Arredondar para 2 casas decimais ao exibir
        irrf_arredondado = round(irrf_calculado, 2)
        irrf_acumulado += irrf_arredondado

        # Ajustar o IRRF na última operação de venda
        if i == len(operacoes_venda) - 1:  # Verificar se é a última operação de venda
            diferenca_irrf = round(irrf_total - irrf_acumulado, 2)
            irrf_arredondado += diferenca_irrf

        # Atualizar o IRRF na própria operação de venda
        op["irrf"] = irrf_arredondado

    # Preencher IRRF nas operações originais considerando a ordem correta de vendas
    indice_venda = 0
    for op in operacoes:
        if op["tipo"] == "V":  # Apenas vendas possuem IRRF
            op["irrf"] = operacoes_venda[indice_venda]["irrf"]
            indice_venda += 1  # Avançar para a próxima venda
        else:
            op["irrf"] = 0.0  # IRRF é 0 para compras

    # Gerar a saída formatada com ajustes para planilha  
    resultado = []  
    for op in operacoes:  
        linha = [  
            op["ticker"],  
            data_pregao,  
            op["tipo"],  
            op["quantidade"],  
            f"{op['preco']:.2f}".replace(".", ","),  # Substituir separador decimal  
            f"{op['taxa']:.2f}".replace(".", ","),  # Taxas arredondadas para 2 casas decimais  
            "BTG",  
            "" if op["irrf"] == 0.0 else f"{op['irrf']:.2f}".replace(".", ","),  # IRRF arredondado  
            "BRL",  
            f"NC:{numero_nota}"  
        ]  
        resultado.append("\t".join(map(str, linha)))  

    # Formatar o resultado como uma string única com quebras de linha  
    resultado_final = "\n".join(resultado)  

    # Salvar no arquivo especificado  
    #with open(output_file, "w", encoding="utf-8") as arquivo:  
    #    arquivo.write(resultado_final)  

    print(resultado_final)
    return resultado_final

@app.route('/test', methods=['GET'])
def test():
    return jsonify({"status": "Backend está funcionando!"})

@app.route('/process', methods=['POST'])
def process_files():
    try:
        if 'files' not in request.files:
            return jsonify({'error': 'Nenhum arquivo enviado'}), 400
        
        files = request.files.getlist('files')
        
        # Verificar se são PDFs
        for file in files:
            if not file.filename.lower().endswith('.pdf'):
                return jsonify({'error': 'Apenas arquivos PDF são permitidos'}), 400
        
        todos_resultados = []
        
        # Criar diretório temporário para salvar os arquivos
        with tempfile.TemporaryDirectory() as temp_dir:
            for i, file in enumerate(files):
                # Salvar arquivo temporariamente
                temp_path = os.path.join(temp_dir, f"temp_{i}.pdf")
                file.save(temp_path)
                print(temp_path)
                
                # Processar arquivo
                try:
                    nome_saida = os.path.join(temp_dir, f"resultado_{i}.txt")
                    resultado = processar_nota_corretagem(temp_path)
                    todos_resultados.append(resultado)
                except Exception as e:
                    return jsonify({'error': f'Erro ao processar arquivo {file.filename}: {str(e)}'}), 500
        
        # Juntar todos os resultados
        resultado_final = "\n\n".join(todos_resultados)
        
        
        return jsonify({'result': resultado_final})

    except Exception as e:
        return jsonify({'error': f'Erro interno: {str(e)}'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5555))
    app.run(host='0.0.0.0', port=port)