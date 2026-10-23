/* xtos_mbedtls_config.h — minimal mbedTLS (3.6 LTS) configuration for the
 * XTOS update tool: an HTTPS *client* (TLS 1.2 + TLS 1.3) that verifies the
 * server against a pinned CA bundle.
 *
 * Everything else is disabled: no server role, no DTLS, no session cache,
 * no PSK-only suites, no private-key operations, no debug, no net_sockets
 * (the tool owns its sockets and uses mbedtls_ssl_set_bio callbacks).
 *
 * Build with -DMBEDTLS_CONFIG_FILE='"xtos_mbedtls_config.h"'.
 */
#ifndef XTOS_MBEDTLS_CONFIG_H
#define XTOS_MBEDTLS_CONFIG_H

/* ---- system ---- */
#define MBEDTLS_HAVE_ASM
#define MBEDTLS_HAVE_TIME
#define MBEDTLS_HAVE_TIME_DATE /* X.509 validity periods are enforced */

/* ---- TLS: stream client, TLS 1.2 + TLS 1.3, ephemeral (certificate) key exchange ---- */
#define MBEDTLS_SSL_TLS_C
#define MBEDTLS_SSL_CLI_C
#define MBEDTLS_SSL_PROTO_TLS1_2
#define MBEDTLS_SSL_PROTO_TLS1_3
#define MBEDTLS_SSL_TLS1_3_KEY_EXCHANGE_MODE_EPHEMERAL_ENABLED
#define MBEDTLS_SSL_SERVER_NAME_INDICATION /* SNI: CDN edges need it for the right cert */
/* TLS 1.2 key exchange */
#define MBEDTLS_KEY_EXCHANGE_ECDHE_RSA_ENABLED
#define MBEDTLS_KEY_EXCHANGE_ECDHE_ECDSA_ENABLED
/* Restrict suites to ECDHE with AES-GCM (RSA + ECDSA server certs)
 * plus the two mandatory TLS 1.3 AES-GCM suites. */
#define MBEDTLS_SSL_CIPHERSUITES \
    MBEDTLS_TLS1_3_AES_256_GCM_SHA384, \
    MBEDTLS_TLS1_3_AES_128_GCM_SHA256, \
    MBEDTLS_TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256, \
    MBEDTLS_TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256, \
    MBEDTLS_TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384, \
    MBEDTLS_TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
#define MBEDTLS_SSL_KEEP_PEER_CERTIFICATE

/* ---- X.509: parse + verify (client role only) ---- */
#define MBEDTLS_ASN1_PARSE_C
#define MBEDTLS_X509_USE_C
#define MBEDTLS_X509_CRT_PARSE_C
#define MBEDTLS_FS_IO /* read the pinned CA bundle file */
#define MBEDTLS_PK_C
#define MBEDTLS_PK_PARSE_C
#define MBEDTLS_OID_C
#define MBEDTLS_BASE64_C
#define MBEDTLS_PEM_PARSE_C

/* ---- public-key crypto ---- */
#define MBEDTLS_BIGNUM_C
#define MBEDTLS_RSA_C
#define MBEDTLS_PKCS1_V15 /* RSA PKCS#1 v1.5 signatures (certificate verify) */
#define MBEDTLS_PKCS1_V21 /* RSA-PSS signatures (modern intermediates) */
#define MBEDTLS_X509_RSASSA_PSS_SUPPORT /* TLS 1.3 RSA-PSS CertificateVerify */
#define MBEDTLS_ECP_C
#define MBEDTLS_ECDH_C
#define MBEDTLS_ECDSA_C
#define MBEDTLS_ECP_DP_SECP256R1_ENABLED
#define MBEDTLS_ECP_DP_SECP384R1_ENABLED
#define MBEDTLS_ECP_DP_X25519_ENABLED /* X25519 key exchange */

/* ---- hashes ---- */
#define MBEDTLS_SHA256_C
#define MBEDTLS_SHA384_C /* TLS 1.3 AES-256-GCM-SHA384 and SHA-384 signatures */
#define MBEDTLS_SHA512_C
#define MBEDTLS_MD_C
#define MBEDTLS_HKDF_C /* TLS 1.3 key schedule (via PSA HKDF derivation) */

/* ---- symmetric cipher ---- */
#define MBEDTLS_CIPHER_C
#define MBEDTLS_AES_C
#define MBEDTLS_GCM_C
#define MBEDTLS_CCM_C

/* ---- RNG for the ephemeral key exchange ---- */
#define MBEDTLS_ENTROPY_C
#define MBEDTLS_HMAC_DRBG_C
#define MBEDTLS_ENTROPY_FORCE_SHA256

/* ---- PSA crypto (required by the TLS stack in 3.6) ----
 * PSA algorithm support is derived from the classic modules above. */
#define MBEDTLS_PSA_CRYPTO_C

/* Diagnostics: debug output is dormant unless a debug callback is attached. */
#define MBEDTLS_DEBUG_C

/* Certificate chains are validated with SHA-256+; system time via time(3). */
#define MBEDTLS_MPI_MAX_SIZE 512

#endif /* XTOS_MBEDTLS_CONFIG_H */
