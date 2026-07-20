cat script.sh > new_install.run
echo "__APOLLO_INSTALL_PAYLOAD_BELOW__" >> new_install.run
cat payload.tar >> new_install.run
tar -cf - -C payload . >> new_install.run

awk '/^__APOLLO_INSTALL_PAYLOAD_BELOW__$/ {exit} {print}' install.run > script.sh

awk '/^__APOLLO_INSTALL_PAYLOAD_BELOW__$/ {flag=1; next} flag' install.run > payload.tar
