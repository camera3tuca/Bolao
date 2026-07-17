# ⚽ Bolão de Futebol — Dashboard

Aplicativo em [Streamlit](https://streamlit.io/) para gerenciar um bolão de
futebol: cadastro de palpites, ranking automático e divisão de prêmios (Pix).
Os dados ficam em um banco PostgreSQL ([Neon](https://neon.tech) ou
[Supabase](https://supabase.com/)).

## Funcionalidades

- **Layout wide com abas** (Registrar Palpite · Ranking · Palpites · Prêmios),
  otimizado para tablet, com indicadores (KPIs) no topo.
- **Registro de palpites** com data/hora (horário de Brasília) e confirmação
  de pagamento (Pix).
- **Ranking automático**: 3 pontos por placar exato, 1 por acertar o vencedor.
- **Controle de pagamento**: status Pago/Pendente, filtro na lista de palpites,
  confirmação pelo admin e **resumo de arrecadação** por partida (esperado ×
  confirmado × pendente).
- **Painel de administração** (senha): criar/encerrar/deletar partidas,
  confirmar pagamentos e deletar palpites.

## Como rodar localmente

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edite .streamlit/secrets.toml e coloque a senha do banco
streamlit run app.py
```

## Configuração do banco (Supabase) — IMPORTANTE

O Supabase **desativou o suporte a IPv4 direto**. O host direto
(`db.<ref>.supabase.co`) só responde por IPv6, e o Streamlit Community Cloud
normalmente só tem IPv4. Por isso a conexão com a URL direta falha com o
erro *"Erro de Conexão com o Supabase"*.

A solução é usar o **Connection Pooler (Supavisor)**, que atende por IPv4.

No Streamlit Cloud, vá em **App settings → Secrets** e cole:

```toml
DATABASE_URL = "postgresql://postgres.<ref>:[YOUR-PASSWORD]@aws-0-<regiao>.pooler.supabase.com:5432/postgres"
```

Para este projeto (ref `lueqftezoesihyecftzq`, região `us-west-2`):

```toml
DATABASE_URL = "postgresql://postgres.lueqftezoesihyecftzq:[YOUR-PASSWORD]@aws-0-us-west-2.pooler.supabase.com:5432/postgres"
```

Onde achar no painel: **Project Settings → Database → Connection pooling**.
Use o **Session pooler** (porta `5432`) para apps persistentes como este; o
**Transaction pooler** (porta `6543`) também funciona.

### Fallback automático

Mesmo que você deixe a **URL direta** nos secrets, o app agora a converte
automaticamente para a URL do pooler e tenta essa primeiro, então a conexão
funciona sem edição manual. Se o seu projeto não estiver em `us-west-2`,
defina a região com:

```toml
SUPABASE_REGION = "sua-regiao"   # ex.: sa-east-1
```

### Alternativa: Neon (também IPv4, funciona direto)

O app funciona com **qualquer PostgreSQL**, incluindo o [Neon](https://neon.tech).
O Neon é uma boa opção para o Streamlit Cloud porque o endpoint **com pooler**
já responde por **IPv4** e a string de conexão já inclui `sslmode=require` — ou
seja, **basta colar a string, sem conversão de pooler nem região**.

No painel do Neon: **Connection Details** → ative **Connection pooling** → copie
a *Connection string* (host termina em `-pooler.<regiao>.aws.neon.tech`) e cole
nos Secrets:

```toml
DATABASE_URL = "postgresql://neondb_owner:[YOUR-PASSWORD]@ep-xxxx-pooler.sa-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
```

Observação: no plano free o Neon **suspende o compute** após inatividade; a
primeira conexão pode levar alguns segundos (o app já faz uma retentativa
automática para esse *cold start*).

### Diagnóstico

Se ainda houver erro de conexão, o app agora mostra a **causa provável**
(senha incorreta, região/tenant errado ou rede/IPv4) e um expander
**"Diagnóstico de conexão"** listando cada tentativa (host/porta/usuário, **com
a senha sempre ocultada**). Isso torna trivial identificar o que corrigir.

## Variáveis suportadas

| Chave (secrets/env)                | Descrição                                               |
| ---------------------------------- | ------------------------------------------------------- |
| `DATABASE_URL` / `SUPABASE_DB_URL` | String de conexão do PostgreSQL (use a URL do pooler).  |
| `SUPABASE_REGION`                  | Região usada ao converter uma URL direta em pooler.     |
| `ADMIN_PASSWORD`                   | Senha do painel de administração (padrão `5075`).       |

O `sslmode=require` é adicionado automaticamente à conexão (o Supabase exige SSL).

## Testes

```bash
python test_app.py       # testes de parsing, ranking e prêmios (pulados sem DB)
```

O painel de administração é liberado com a senha definida em `app.py`.
