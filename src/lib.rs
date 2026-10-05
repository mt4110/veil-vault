use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;
use uuid::{Uuid, Variant, Version};
use worker::*;

const MAX_PAYLOAD_BYTES: usize = 65_536;
const TTL_SECONDS: u32 = 86_400;

const INSERT_SECRET: &str = "INSERT INTO secrets (id, payload) VALUES (?1, ?2)";

// A single write statement: never SELECT followed by DELETE.
// Expired records are consumed too, but their ciphertext never leaves D1.
const CONSUME_SECRET: &str = "DELETE FROM secrets WHERE id = ?1
    RETURNING CASE WHEN created_at > unixepoch() - 86400
        THEN payload ELSE NULL END AS payload";

const CLEANUP_EXPIRED: &str = "DELETE FROM secrets WHERE created_at <= unixepoch() - 86400";

#[derive(Serialize)]
struct CreatedSecret {
    id: String,
    expires_in_seconds: u32,
}

#[derive(Deserialize)]
struct ConsumedSecret {
    payload: Option<String>,
}

#[derive(Serialize)]
struct ApiError<'a> {
    error: &'a str,
}

#[event(fetch)]
pub async fn main(req: Request, env: Env, _ctx: Context) -> Result<Response> {
    // Do not expose D1 errors: they can contain SQL or bound ciphertext.
    let response = match route(req, env).await {
        Ok(response) => response,
        Err(_) => api_error(500, "internal_server_error")?,
    };
    protect_response(response)
}

async fn route(req: Request, env: Env) -> Result<Response> {
    // Missing or unexpected configuration closes the API before touching D1.
    if !matches!(
        env.var("SERVICE_ENABLED")
            .map(|value| value.to_string())
            .as_deref(),
        Ok("true")
    ) {
        return api_error(503, "service_unavailable");
    }
    let path = req.path();
    if path == "/api/secrets" {
        return match req.method() {
            Method::Post => {
                if let Some(response) = authorize_publisher(&req, &env)? {
                    return Ok(response);
                }
                create_secret(req, env).await
            }
            _ => method_not_allowed("POST"),
        };
    }

    if let Some(id) = path.strip_prefix("/api/secrets/") {
        if id.is_empty() || id.contains('/') {
            return api_error(404, "not_found");
        }
        // HEAD/OPTIONS/link inspection must never consume a record.
        if req.method() != Method::Get {
            return method_not_allowed("GET");
        }
        let Ok(parsed) = Uuid::parse_str(id) else {
            return api_error(400, "invalid_id");
        };
        if parsed.get_variant() != Variant::RFC4122
            || parsed.get_version() != Some(Version::Random)
            || !parsed.hyphenated().to_string().eq_ignore_ascii_case(id)
        {
            return api_error(400, "invalid_id");
        }
        return consume_secret(parsed.to_string(), env).await;
    }

    api_error(404, "not_found")
}

fn authorize_publisher(req: &Request, env: &Env) -> Result<Option<Response>> {
    let Ok(secret) = env.secret("PUBLISH_TOKEN") else {
        return Ok(Some(api_error(503, "service_unavailable")?));
    };
    let expected = secret.to_string();
    // Fixed public format: 32 cryptographically random bytes encoded as lowercase hex.
    if !valid_publish_token(&expected) {
        return Ok(Some(api_error(503, "service_unavailable")?));
    }
    let header = req.headers().get("Authorization")?.unwrap_or_default();
    let supplied = header
        .split_once(' ')
        .and_then(|(scheme, token)| scheme.eq_ignore_ascii_case("Bearer").then_some(token));
    let authorized = supplied.is_some_and(|token| {
        valid_publish_token(token) && bool::from(expected.as_bytes().ct_eq(token.as_bytes()))
    });
    if authorized {
        return Ok(None);
    }
    let mut response = api_error(401, "unauthorized")?;
    response.headers_mut().set("WWW-Authenticate", "Bearer")?;
    Ok(Some(response))
}

fn valid_publish_token(token: &str) -> bool {
    token.len() == 64
        && token
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

async fn create_secret(mut req: Request, env: Env) -> Result<Response> {
    let content_type = req.headers().get("Content-Type")?.unwrap_or_default();
    if !content_type
        .split(';')
        .next()
        .is_some_and(|media_type| media_type.trim().eq_ignore_ascii_case("text/plain"))
    {
        return api_error(415, "expected_text_plain_utf8");
    }

    // Content-Length is only an early rejection; enforce the bound on the stream too.
    if let Some(length) = req.headers().get("Content-Length")? {
        let Ok(length) = length.parse::<u64>() else {
            return api_error(400, "invalid_content_length");
        };
        if length > MAX_PAYLOAD_BYTES as u64 {
            return api_error(413, "payload_too_large");
        }
    }

    let Ok(mut stream) = req.stream() else {
        return api_error(400, "invalid_body");
    };
    let mut body = Vec::new();
    while let Some(chunk) = stream.next().await {
        let Ok(chunk) = chunk else {
            return api_error(400, "invalid_body");
        };
        if chunk.len() > MAX_PAYLOAD_BYTES - body.len() {
            return api_error(413, "payload_too_large");
        }
        body.extend_from_slice(&chunk);
    }

    let Ok(payload) = String::from_utf8(body) else {
        return api_error(400, "invalid_utf8");
    };
    if payload.trim().is_empty() {
        return api_error(400, "empty_payload");
    }

    // UUID v4 uses the runtime's secure randomness through uuid's `js` feature.
    let id = Uuid::new_v4().to_string();
    let db = env.d1("DB")?;
    let result = db
        .prepare(INSERT_SECRET)
        .bind(&[id.clone().into(), payload.into()])?
        .run()
        .await?;
    if !result.success() {
        return api_error(500, "internal_server_error");
    }

    Ok(Response::from_json(&CreatedSecret {
        id,
        expires_in_seconds: TTL_SECONDS,
    })?
    .with_status(201))
}

async fn consume_secret(id: String, env: Env) -> Result<Response> {
    let db = env.d1("DB")?;
    // Await the write before constructing a successful response. Never retry a consume.
    let record = db
        .prepare(CONSUME_SECRET)
        .bind(&[id.into()])?
        .first::<ConsumedSecret>(None)
        .await?;

    match record.and_then(|record| record.payload) {
        Some(payload) => {
            let mut response = Response::ok(payload)?;
            response
                .headers_mut()
                .set("Content-Type", "text/plain; charset=utf-8")?;
            Ok(response)
        }
        None => api_error(404, "not_found"),
    }
}

#[event(scheduled)]
pub async fn scheduled(_event: ScheduledEvent, env: Env, _ctx: ScheduleContext) {
    // TTL is enforced by the consume SQL even if this scheduled cleanup fails.
    let cleanup = async {
        let result = env.d1("DB")?.prepare(CLEANUP_EXPIRED).run().await?;
        if !result.success() {
            return Err(Error::RustError("cleanup failed".into()));
        }
        Ok::<(), Error>(())
    };
    if cleanup.await.is_err() {
        console_error!("expired-record cleanup failed");
    }
}

fn api_error(status: u16, error: &str) -> Result<Response> {
    Ok(Response::from_json(&ApiError { error })?.with_status(status))
}

fn method_not_allowed(allow: &str) -> Result<Response> {
    let mut response = api_error(405, "method_not_allowed")?;
    response.headers_mut().set("Allow", allow)?;
    Ok(response)
}

fn protect_response(mut response: Response) -> Result<Response> {
    let headers = response.headers_mut();
    headers.set("Cache-Control", "no-store")?;
    headers.set("Referrer-Policy", "no-referrer")?;
    headers.set("X-Content-Type-Options", "nosniff")?;
    Ok(response)
}
