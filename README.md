# ⚽ Bolão de Futebol — Dashboard

Aplicativo em [Streamlit](https://streamlit.io/) para gerenciar um bolão de
futebol: cadastro de palpites, ranking automático e divisão de prêmios (Pix).
Os dados ficam em um banco PostgreSQL do [Supabase](https://supabase.com/).

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

## Variáveis suportadas

| Chave (secrets/env)                | Descrição                                              |
| ---------------------------------- | ------------------------------------------------------ |
| `DATABASE_URL` / `SUPABASE_DB_URL` | String de conexão do PostgreSQL (use a URL do pooler). |
| `SUPABASE_REGION`                  | Região usada ao converter uma URL direta em pooler.    |

## Testes

```bash
python test_app.py       # testes de parsing, ranking e prêmios (pulados sem DB)
```

O painel de administração é liberado com a senha definida em `app.py`.
