(comment) @comment
(string) @string
(number) @number
(lifetime) @label
(attribute) @attribute
(attribute (identifier) @attribute)
(verbatim_block tag: (identifier) @type)
(verbatim_block (verbatim_word) @keyword)
(macro_call name: (identifier) @function.macro)

(declaration "interface" @keyword name: (identifier) @type)
(declaration "struct" @keyword name: (identifier) @type)
(declaration "enum" @keyword name: (identifier) @type)
(declaration "type" @keyword name: (identifier) @type)
(declaration "impl" @keyword name: (identifier) @type)
(declaration "fn" @keyword name: (identifier) @function)
(declaration "method" @keyword name: (identifier) @function)
(declaration "macro" @keyword name: (identifier) @function.macro)
(declaration "const" @keyword name: (identifier) @constant)
(declaration "namespace" @keyword name: (identifier) @module)

; GEN-BEGIN words
["as" "break" "cfg" "const" "continue" "else" "enum" "export" "extern" "fn" "for" "gen" "if" "impl"
 "in" "let" "loop" "macro" "match" "mut" "private" "return" "struct" "type" "unsafe" "use" "while"] @keyword

; contextual words, types and constants are plain identifiers to the grammar
((identifier) @keyword (#any-of? @keyword "after" "before" "class" "default" "foreign" "import" "include" "interface" "method" "namespace" "override" "static"))
((identifier) @type.builtin (#any-of? @type.builtin "void" "bool" "char" "i8" "i16" "i32" "i64" "u8" "u16" "u32" "u64" "usize" "isize"))
((identifier) @constant.builtin (#any-of? @constant.builtin "true" "false"))
((identifier) @variable.builtin (#any-of? @variable.builtin "self" "Self"))
; GEN-END words
