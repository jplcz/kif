; The body of header(cxx) { } / code(cxx) { } is C++; any other tag, or none, is C.
((verbatim_block tag: (identifier) @_tag (verbatim_body) @injection.content)
 (#eq? @_tag "cxx")
 (#set! injection.language "cpp"))

((verbatim_block !tag (verbatim_body) @injection.content)
 (#set! injection.language "c"))

((verbatim_block tag: (identifier) @_tag (verbatim_body) @injection.content)
 (#not-eq? @_tag "cxx")
 (#set! injection.language "c"))
