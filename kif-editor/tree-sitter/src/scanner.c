// External scanner for kif: the body of header { } / code { } blocks is raw text that ends at the first line
// consisting only of '}' (plus optional blanks), so braces inside the embedded C do not matter.
#include "tree_sitter/parser.h"

enum TokenType { VERBATIM_BODY };

void *tree_sitter_kif_external_scanner_create(void) { return NULL; }
void tree_sitter_kif_external_scanner_destroy(void *payload) { (void)payload; }
unsigned tree_sitter_kif_external_scanner_serialize(void *payload, char *buffer) {
  (void)payload;
  (void)buffer;
  return 0;
}
void tree_sitter_kif_external_scanner_deserialize(void *payload, const char *buffer, unsigned length) {
  (void)payload;
  (void)buffer;
  (void)length;
}

bool tree_sitter_kif_external_scanner_scan(void *payload, TSLexer *lexer, const bool *valid_symbols) {
  (void)payload;
  if (!valid_symbols[VERBATIM_BODY])
    return false;

  bool any = false;
  bool first_line = true; // the rest of the line holding '{' is part of the body, never a terminator
  lexer->mark_end(lexer);

  for (;;) {
    if (!first_line && lexer->lookahead == '}') {
      lexer->advance(lexer, false);
      while (lexer->lookahead == ' ' || lexer->lookahead == '\t')
        lexer->advance(lexer, false);
      if (lexer->lookahead == '\n' || lexer->lookahead == '\r' || lexer->eof(lexer)) {
        lexer->result_symbol = VERBATIM_BODY;
        return any; // token ends at the last mark, i.e. before the closing '}' line
      }
    }
    first_line = false;

    while (lexer->lookahead != '\n' && !lexer->eof(lexer))
      lexer->advance(lexer, false);
    if (lexer->eof(lexer)) {
      lexer->mark_end(lexer);
      lexer->result_symbol = VERBATIM_BODY;
      return true; // unterminated block: let the parser report the missing '}'
    }
    lexer->advance(lexer, false); // the '\n'
    lexer->mark_end(lexer);
    any = true;
  }
}
