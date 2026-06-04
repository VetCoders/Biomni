# Minimal makefile for Sphinx documentation
#

# You can set these variables from the command line, and also
# from the environment for the first two.
SPHINXOPTS    ?=
SPHINXBUILD   ?= sphinx-build
SOURCEDIR     = source
BUILDDIR      = build

HELP_C_CYAN   := \033[36m
HELP_C_GREEN  := \033[32m
HELP_C_YELLOW := \033[33m
HELP_C_RESET  := \033[0m

# Put it first so that "make" without argument is like "make help".
help:
	@printf "\n$(HELP_C_CYAN)biomni-portal docs$(HELP_C_RESET)\n"
	@printf "$(HELP_C_YELLOW)Sphinx targets$(HELP_C_RESET): use $(HELP_C_GREEN)make html$(HELP_C_RESET), $(HELP_C_GREEN)make clean$(HELP_C_RESET), or any Sphinx builder below.\n\n"
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help Makefile

# Catch-all target: route all unknown targets to Sphinx using the new
# "make mode" option.  $(O) is meant as a shortcut for $(SPHINXOPTS).

html:
	@printf "$(HELP_C_YELLOW)[1/2]$(HELP_C_RESET) Generating API documentation...\n"
	@python generate_api_docs.py

	@printf "$(HELP_C_YELLOW)[2/2]$(HELP_C_RESET) Building HTML documentation with Sphinx...\n"
	@$(SPHINXBUILD) -M html "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
